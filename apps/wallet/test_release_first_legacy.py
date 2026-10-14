"""Characterize the unresolved legacy boundary; never forge a disposition.

Already persisted blockers and real active unknowns keep admission closed.
Unmapped closed history must not gain new account restrictions during upgrade.
"""
from decimal import Decimal
from importlib import import_module
from unittest.mock import patch
from django.apps import apps
from django.db import connection
from django.test import TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError
from apps.orders.models import Order, OrderStatusLog
from apps.orders.test_release_first import OfflineAdapter
from apps.payments.models import Payment, Refund
from apps.wallet import test_order_durable as fixtures
from apps.wallet.coin_balance_service import pay_order_with_coin_aware_balance as pay
from apps.wallet.coin_sync import coin_backed_wallet_amount
from apps.wallet.models import ClientWalletLedger, WalletSpendAttempt, RechargeOrder
from apps.wallet.spend_models import OrderWalletSpend
from apps.wallet import spend_service as spend


@override_settings(ORDER_SURCHARGE_ISOLATED_TEST_MODE=False,
    SHARED_SPEND_PLATFORM_APPROVED=True, WECHAT_VIRTUALPAY_ENABLED=True,
    WECHAT_VIRTUALPAY_COIN_UNITS_PER_YUAN=10, WECHAT_VIRTUALPAY_ENV=1)
class LegacyAdmissionBoundaryTests(TransactionTestCase):
    def setUp(self):
        fixtures.OriginalOrderDurableTests.setUp(self)

    def new_order(self, number):
        return Order.objects.create(order_no=number, boss_user=self.user, package=self.package,
            required_players=1, total_price_per_hour=6, total_amount=6, status=Order.STATUS_PENDING_PAYMENT)

    def legacy(self):
        Order.objects.filter(pk=self.order.pk).update(status=Order.STATUS_CANCELLED)
        self.order.refresh_from_db()
        OrderStatusLog.objects.create(order=self.order, from_status=Order.STATUS_WAITING,
            to_status=Order.STATUS_PENDING_PAYMENT, reason='synthetic historical evidence')
        # Explicit pre-existing evidence fixture, NOT a migration inference from
        # closed history. Existing immutable blockers still must be respected.
        return OrderWalletSpend.objects.create(order=self.order, blocker='LEGACY_COIN_REVIEW_REQUIRED',
            intent={'version':'synthetic-persisted-blocker', 'prior_status':Order.STATUS_CANCELLED,
                'coin_recharge_nos':['DURABLE-SOURCE']})

    def test_cancelled_history_keeps_original_evidence_and_new_spends_closed_for_all_remote_outcomes(self):
        binding = self.legacy()
        frozen = list(OrderWalletSpend.objects.filter(pk=binding.pk).values())
        new = self.new_order('NEW-AFTER-LEGACY')
        for outcome in ('success', 'refused', 'unknown'):
            adapter = OfflineAdapter(outcome=outcome)
            with self.subTest(outcome=outcome), patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter) as factory:
                for order in (self.order, new):
                    with self.assertRaises(ValidationError) as caught:
                        pay(order.order_no, self.user, code='offline')
                    self.assertEqual(str(caught.exception.detail['code']), 'LEGACY_COIN_REVIEW_REQUIRED')
                factory.assert_not_called()
                self.assertEqual((adapter.auth_calls, adapter.spend_calls), (0, 0))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('10'))
        self.assertEqual(coin_backed_wallet_amount(self.profile), Decimal('10'))
        self.assertEqual(frozen, list(OrderWalletSpend.objects.filter(pk=binding.pk).values()))
        self.assertFalse(WalletSpendAttempt.objects.exists())
        self.assertFalse(Payment.objects.exists()); self.assertFalse(Refund.objects.exists())
        self.assertEqual(ClientWalletLedger.objects.count(), 1)
        # Both no old debit and an old remote debit with local rollback yield
        # precisely these rows. This 10 is local provenance, NOT remote proof.
        self.assertEqual(binding.intent['prior_status'], Order.STATUS_CANCELLED)
        self.assertEqual(binding.intent['coin_recharge_nos'], ['DURABLE-SOURCE'])

    def test_legacy_cannot_bypass_with_cash_helper_or_other_spend_kinds(self):
        from apps.wallet.services import pay_order_with_balance
        self.legacy(); new = self.new_order('NO-CASH-BYPASS')
        with self.assertRaises(ValidationError) as caught:
            pay_order_with_balance(new.order_no, self.user)
        self.assertEqual(str(caught.exception.detail['code']), 'LEGACY_COIN_REVIEW_REQUIRED')
        for kind in ('order', 'order_checkout', 'gift', 'surcharge'):
            with self.subTest(kind=kind), self.assertRaises(ValidationError) as caught:
                spend.reserve(self.profile, kind=kind, business_no=new.order_no,
                    key=kind, amount=Decimal('6'), intent={})
            self.assertEqual(str(caught.exception.detail['code']), 'LEGACY_COIN_REVIEW_REQUIRED')
        self.assertFalse(WalletSpendAttempt.objects.exists())
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('10'))

    def test_closed_history_does_not_create_new_account_restrictions(self):
        Order.objects.filter(pk=self.order.pk).update(status=Order.STATUS_CANCELLED)
        OrderStatusLog.objects.create(order=self.order, from_status=Order.STATUS_WAITING,
            to_status=Order.STATUS_PENDING_PAYMENT, reason='synthetic old closed order')
        before_ledger = list(ClientWalletLedger.objects.order_by('pk').values())
        module = import_module('apps.wallet.migrations.0014_original_order_admission_guards')
        with connection.schema_editor() as editor:
            module.quarantine_legacy(apps, editor)
        self.assertFalse(OrderWalletSpend.objects.filter(order=self.order).exists())
        self.assertEqual(list(ClientWalletLedger.objects.order_by('pk').values()), before_ledger)
        new = self.new_order('NEW-AFTER-CLOSED')
        adapter = OfflineAdapter()
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter):
            self.assertEqual(pay(new.order_no, self.user, code='offline')['status'], 'paid')
        self.assertEqual(adapter.spend_calls, 1)
        self.assertFalse(OrderWalletSpend.objects.filter(order=self.order).exists())

    def test_clean_new_order_mixed_source_success_uses_actual_capture_not_local_guess(self):
        # 4 coin + 6 evidenced local; unlike ambiguous legacy there is no blocker.
        RechargeOrder.objects.filter(recharge_no='DURABLE-SOURCE').update(amount=4)
        ClientWalletLedger.objects.filter(reference_id='DURABLE-SOURCE').update(amount=4, balance_after=4)
        ClientWalletLedger.objects.create(wallet=self.wallet, entry_type='admin_adjust',
            amount=6, balance_after=10, reference_id='offline-local')
        adapter = OfflineAdapter()
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter):
            result = pay(self.order.order_no, self.user, code='offline')
            again = pay(self.order.order_no, self.user, code='offline')
        self.assertEqual((result['status'], again['status']), ('paid', 'paid'))
        attempt = WalletSpendAttempt.objects.get()
        self.assertEqual((Decimal(attempt.source_snapshot['coin_amount']),
            Decimal(attempt.source_snapshot['local_amount']), attempt.source_snapshot['coin_units']),
            (Decimal('4'), Decimal('2'), 40))
        self.assertEqual(attempt.source_snapshot['allocations'][0]['reference_id'], 'DURABLE-SOURCE')
        self.assertEqual(adapter.spend_calls, 1)
        self.assertEqual(coin_backed_wallet_amount(self.profile), Decimal('0'))
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('4'))

    def check_unproven_remote(self, outcome):
        from apps.wallet.services import pay_order_with_balance
        adapter = OfflineAdapter(outcome=outcome)
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter):
            for _ in range(2):
                self.assertEqual(pay(self.order.order_no, self.user, code='offline')['status'], 'unknown')
        attempt = WalletSpendAttempt.objects.get()
        self.assertEqual((attempt.status, attempt.reserved_amount), ('unknown', Decimal('6')))
        self.assertEqual(Decimal(attempt.source_snapshot['coin_amount']), Decimal('6'))
        self.assertEqual(Decimal(attempt.source_snapshot['local_amount']), Decimal('0'))
        self.assertEqual(attempt.source_snapshot['coin_units'], 60)
        self.assertEqual(adapter.spend_calls, 1)
        new = self.new_order('UNKNOWN-BLOCKS')
        with self.assertRaises(ValidationError) as caught:
            pay_order_with_balance(new.order_no, self.user)
        self.assertEqual(str(caught.exception.detail['code']), 'PAYMENT_PENDING')
        with self.assertRaises(ValidationError) as caught:
            spend.reserve(self.profile, kind='order', business_no=new.order_no, key='new', amount=Decimal('1'), intent={})
        self.assertEqual(str(caught.exception.detail['code']), 'PAYMENT_PENDING')
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('10'))
        self.assertFalse(Payment.objects.exists()); self.assertFalse(Refund.objects.exists())

    def test_clean_new_order_remote_refusal_is_not_assumed_failed_or_released(self):
        self.check_unproven_remote('refused')

    def test_active_true_unknown_never_releases_or_allows_cash_bypass(self):
        self.check_unproven_remote('unknown')

    def test_other_checkout_source_remains_reserved_even_with_local_cash(self):
        from apps.wallet.services import pay_order_with_balance
        other = self.new_order('SOURCE-OWNER')
        RechargeOrder.objects.filter(recharge_no='DURABLE-SOURCE').update(checkout_order_no=other.order_no)
        with self.assertRaises(ValidationError) as caught:
            pay(self.order.order_no, self.user, code='offline')
        self.assertEqual(str(caught.exception.detail['code']), 'CHECKOUT_RECOVERY_PENDING')
        with self.assertRaises(ValidationError) as caught:
            pay_order_with_balance(self.order.order_no, self.user)
        self.assertEqual(str(caught.exception.detail['code']), 'COIN_USE_DURABLE_PAYMENT')
        self.assertFalse(WalletSpendAttempt.objects.exists())
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('10'))
