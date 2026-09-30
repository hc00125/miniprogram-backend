"""Original order durable-spend acceptance; fake boundary, real DB effects."""
from decimal import Decimal
from unittest.mock import patch
from io import StringIO
from django.contrib.auth.models import User
from django.db import connection
from django.core.management import call_command
from django.test import TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError
from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order
from apps.payments.models import Payment
from .models import ClientWallet, ClientWalletLedger, RechargeOrder, WalletSpendAttempt
from .coin_balance_service import pay_order_with_coin_aware_balance as pay
from . import spend_service as spend


class RecordingAdapter:
    def __init__(self, case, timeout=False):
        self.case, self.timeout = case, timeout
        self.calls = []
    def authenticate(self, attempt, code):
        self.case.assertFalse(connection.in_atomic_block)
        return 'ephemeral-not-persisted'
    def spend(self, attempt, session):
        self.case.assertFalse(connection.in_atomic_block)
        saved = WalletSpendAttempt.objects.get(pk=attempt.pk)
        self.case.assertEqual(saved.status, 'dispatching')
        self.case.assertEqual(saved.request_payload, attempt.request_payload)
        self.case.assertEqual(saved.reserved_amount, Decimal('6.00'))
        self.calls.append(dict(saved.request_payload))
        if self.timeout:
            raise TimeoutError('remote result lost')
        return {'errcode': 0, 'order_id': saved.external_id}
    def query(self, attempt):
        return None


@override_settings(WECHAT_VIRTUALPAY_ENV=1, WECHAT_VIRTUALPAY_ENABLED=True,
                   SHARED_SPEND_PLATFORM_APPROVED=True, WECHAT_VIRTUALPAY_COIN_UNITS_PER_YUAN=10)
class OriginalOrderDurableTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('durable-buyer')
        self.profile = ClientProfile.objects.create(user=self.user, openid='offline-durable')
        self.wallet, _ = ClientWallet.objects.update_or_create(profile=self.profile, defaults={'balance': Decimal('10.00')})
        self.package = Package.objects.create(name='durable', base_price=6, player_count=1)
        self.order = Order.objects.create(order_no='DURABLE-ORDER', boss_user=self.user,
            package=self.package, required_players=1, total_price_per_hour=6, total_amount=6,
            status=Order.STATUS_PENDING_PAYMENT)
        RechargeOrder.objects.create(recharge_no='DURABLE-SOURCE', profile=self.profile,
            amount=Decimal('10'), status='credited', notify_payload={
                'mode': 'short_series_coin', 'wechat_coin_units_per_yuan': 10})
        ClientWalletLedger.objects.create(wallet=self.wallet, entry_type='recharge', amount=Decimal('10'),
            balance_after=Decimal('10'), reference_id='DURABLE-SOURCE')

    def runpay(self, adapter=None, **kwargs):
        adapter = adapter or RecordingAdapter(self)
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter):
            return pay(self.order.order_no, self.user, code='synthetic', **kwargs)

    def test_original_external_spend_is_after_commit_and_persisted(self):
        def legacy_xpay(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block, 'currency_pay inside local transaction')
            return {'errcode': 0, 'balance': 100}
        with patch('apps.wallet.coin_sync.exchange_code_for_session', return_value=(self.profile.openid, 'fake')), \
             patch('apps.wallet.coin_sync.user_xpay_post', side_effect=legacy_xpay):
            adapter = RecordingAdapter(self)
            result = self.runpay(adapter)
        self.assertEqual(result['status'], 'paid')
        self.assertEqual(len(adapter.calls), 1)
        attempt = WalletSpendAttempt.objects.get(kind='order')
        self.assertEqual(adapter.calls[0]['amount'], 60)
        self.assertEqual(attempt.source_snapshot['allocations'][0]['reference_id'], 'DURABLE-SOURCE')
        self.assertEqual(Payment.objects.get(order=self.order).notify_payload['wallet_attempt'], attempt.external_id)

    def test_timeout_retry_different_keys_keeps_attempt_payload_source(self):
        adapter = RecordingAdapter(self, timeout=True)
        self.runpay(adapter, idempotency_key='first')
        a = WalletSpendAttempt.objects.get(kind='order')
        frozen = (a.external_id, a.request_digest, a.source_snapshot, a.request_payload)
        self.runpay(adapter, idempotency_key='another')
        spend.recover(a.pk)
        a.refresh_from_db()
        self.assertEqual(a.status, 'unknown')
        self.assertEqual(a.reserved_amount, Decimal('6'))
        self.assertEqual(frozen, (a.external_id, a.request_digest, a.source_snapshot, a.request_payload))
        self.assertEqual(len(adapter.calls), 1)
        self.assertFalse(Payment.objects.filter(order=self.order).exists())
        self.assertEqual(WalletSpendAttempt.objects.count(), 1)

    def test_remote_success_local_failure_command_restart_exactly_once(self):
        adapter = RecordingAdapter(self)
        with patch('apps.wallet.order_spend.fulfill', side_effect=RuntimeError('injected finalize')):
            with self.assertRaises(RuntimeError):
                self.runpay(adapter)
        a = WalletSpendAttempt.objects.get(kind='order')
        self.assertEqual(a.status, 'succeeded')
        self.assertFalse(Payment.objects.filter(order=self.order).exists())
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        with override_settings(SHARED_SPEND_PLATFORM_APPROVED=False):
            for _ in range(2):
                call_command('reconcile_wallet_spends', apply=True, attempt_id=a.pk, stdout=StringIO())
        self.wallet.refresh_from_db(); self.order.refresh_from_db(); a.refresh_from_db()
        self.assertEqual((a.status, self.wallet.balance, self.order.paid), ('completed', Decimal('4'), True))
        self.assertEqual(Payment.objects.filter(order=self.order).count(), 1)
        self.assertEqual(ClientWalletLedger.objects.filter(reference_id=a.external_id).count(), 1)
        self.assertEqual(len(adapter.calls), 1)

    def test_bad_code_cancels_undispatched_without_spend(self):
        adapter = RecordingAdapter(self)
        with patch.object(adapter, 'authenticate', side_effect=ValidationError('bad code')):
            with self.assertRaises(ValidationError): self.runpay(adapter)
        a = WalletSpendAttempt.objects.get(kind='order')
        self.assertEqual((a.status, a.reserved_amount, adapter.calls), ('failed', Decimal('0'), []))

    def test_platform_closed_zero_external_calls(self):
        with override_settings(SHARED_SPEND_PLATFORM_APPROVED=False), \
             patch('apps.wallet.spend_adapter.user_xpay_post') as external:
            with self.assertRaises(ValidationError): pay(self.order.order_no, self.user, code='bad')
        external.assert_not_called()
        a = WalletSpendAttempt.objects.get(kind='order')
        self.assertEqual(a.status, 'failed')

    def test_first_dispatch_revalidates_price_identity_status_and_eligibility(self):
        from .order_spend import prepare, fulfill
        a = prepare(self.order.order_no, self.user)
        Order.objects.filter(pk=self.order.pk).update(total_amount=7)
        adapter = RecordingAdapter(self)
        with self.assertRaises(ValidationError): spend.execute(a.pk, adapter=adapter, apply=fulfill)
        a.refresh_from_db()
        self.assertEqual((a.status, adapter.calls), ('failed', []))

    def test_unknown_blocks_cancellation_before_refund(self):
        from rest_framework.test import APIClient
        adapter = RecordingAdapter(self, timeout=True)
        self.runpay(adapter)
        client = APIClient(); client.force_authenticate(self.user)
        with patch('apps.payments.refund_integrity.ensure_payment_refund') as refund:
            response = client.post('/api/boss/order/' + self.order.order_no + '/cancel', {}, format='json')
        self.assertEqual(response.status_code, 409, response.data)
        refund.assert_not_called()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_PENDING_PAYMENT)

    def test_own_checkout_reservation_is_usable_other_orders_are_not(self):
        RechargeOrder.objects.filter(recharge_no='DURABLE-SOURCE').update(checkout_order_no=self.order.order_no)
        self.assertEqual(self.runpay()['status'], 'paid')

    def test_prepared_recovery_releases_without_dispatch(self):
        from .order_spend import prepare
        a = prepare(self.order.order_no, self.user)
        self.assertEqual(spend.recover(a.pk).status, 'failed')
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('10'))

    def test_legacy_blocker_never_mints_new_attempt_or_spends(self):
        from .spend_models import OrderWalletSpend
        OrderWalletSpend.objects.create(order=self.order, blocker='LEGACY_COIN_REVIEW_REQUIRED',
            intent={'reason': 'pre-durable uncertain spend'})
        with self.assertRaises(ValidationError): self.runpay()
        self.assertEqual(WalletSpendAttempt.objects.count(), 0)
        with self.assertRaises(ValidationError):
            spend.reserve(self.profile, kind='gift', business_no='blocked', key='blocked',
                amount=Decimal('1'), intent={})

    def test_cash_service_cannot_bypass_coin_or_shared_reservation(self):
        from .services import pay_order_with_balance
        a = spend.reserve(self.profile, kind='gift', business_no='hold', key='hold', amount=Decimal('6'), intent={})
        with self.assertRaises(ValidationError): pay_order_with_balance(self.order.order_no, self.user)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('10'))
        self.assertEqual(a.status, 'prepared')

    def test_zero_checkout_tracks_durable_payment_by_key(self):
        from apps.orders.surcharge_models import OrderCheckout
        from apps.orders.checkout import checkout_data
        item = OrderCheckout.objects.create(order=self.order, boss=self.user, idempotency_key='zero',
            request_digest='a'*64, base_amount=6, total_amount=6,
            quote_snapshot={'quote_version': 'v', 'surcharge': {'amount_diamonds': 0}})
        self.runpay()
        item.refresh_from_db()
        self.assertIsNotNone(item.attempt_id)
        self.assertEqual(checkout_data(self.order)['payment_status'], 'paid')
        self.order.refresh_from_db()
        self.assertFalse(self.order.surcharge_guarded)

    def test_account_revoked_after_auth_before_dispatch_zero_spend(self):
        adapter = RecordingAdapter(self)
        def revoke(attempt, code):
            ClientProfile.objects.filter(pk=self.profile.pk).update(account_status='banned')
            return 'ephemeral'
        with patch.object(adapter, 'authenticate', side_effect=revoke):
            with self.assertRaises(ValidationError): self.runpay(adapter)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(WalletSpendAttempt.objects.get().status, 'failed')

    def test_prepared_cancel_and_local_failure_roll_back_together(self):
        from .order_spend import prepare
        from apps.orders.services import cancel_order
        a = prepare(self.order.order_no, self.user)
        with patch('apps.orders.models.Order.save', side_effect=RuntimeError('cancel failed')):
            with self.assertRaises(RuntimeError): cancel_order(self.order)
        a.refresh_from_db(); self.order.refresh_from_db()
        self.assertEqual(a.status, 'prepared')
        self.assertEqual(self.order.status, Order.STATUS_PENDING_PAYMENT)
        cancel_order(self.order)
        a.refresh_from_db(); self.order.refresh_from_db()
        self.assertEqual(a.status, 'failed')
        self.assertEqual(self.order.status, Order.STATUS_CANCELLED)

    def test_order_query_recovers_success_without_reauthentication(self):
        from apps.orders.payment_views import reconcile_pending_virtual_payment
        with patch('apps.wallet.order_spend.fulfill', side_effect=RuntimeError('local fail')):
            with self.assertRaises(RuntimeError): self.runpay()
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter.authenticate', side_effect=AssertionError('no auth')):
            self.assertTrue(reconcile_pending_virtual_payment(self.order, self.user))
        self.order.refresh_from_db()
        self.assertTrue(self.order.paid)

    def test_unknown_not_ttl_expired_even_without_remote_verification(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.orders.payment_deadlines import expire_due_unpaid_order
        self.runpay(RecordingAdapter(self, timeout=True))
        self.assertFalse(expire_due_unpaid_order(self.order, now=timezone.now()+timedelta(days=20), verify_remote=False))
        a = WalletSpendAttempt.objects.get()
        self.assertEqual((a.status, a.reserved_amount), ('unknown', Decimal('6')))

    def test_cash_only_old_order_pay_then_no_surcharge_marker(self):
        RechargeOrder.objects.all().delete()
        self.assertEqual(self.runpay()['status'], 'paid')
        self.order.refresh_from_db()
        self.assertFalse(self.order.surcharge_guarded)

    def test_cash_only_can_spend_free_value_while_other_checkout_is_reserved(self):
        RechargeOrder.objects.filter(recharge_no='DURABLE-SOURCE').update(amount=7, notify_payload={})
        ClientWalletLedger.objects.filter(reference_id='DURABLE-SOURCE').update(amount=7, balance_after=7)
        other = Order.objects.create(order_no='CASH-RESERVED', boss_user=self.user, package=self.package,
            required_players=1, total_price_per_hour=3, total_amount=3, status=Order.STATUS_PENDING_PAYMENT)
        RechargeOrder.objects.create(recharge_no='CASH-RESERVATION', profile=self.profile,
            amount=Decimal('3'), status='credited', checkout_order_no=other.order_no)
        ClientWalletLedger.objects.create(wallet=self.wallet, entry_type='recharge', amount=Decimal('3'),
            balance_after=Decimal('10'), reference_id='CASH-RESERVATION')
        self.assertEqual(self.runpay()['status'], 'paid')
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('4'))
        from .coin_balance_service import _reserved_checkout_amount
        self.assertEqual(_reserved_checkout_amount(self.profile), Decimal('3'))
        self.assertFalse(WalletSpendAttempt.objects.exists())

    def test_identity_revalidated_after_prepare(self):
        from .order_spend import prepare, fulfill
        a = prepare(self.order.order_no, self.user)
        ClientProfile.objects.filter(pk=self.profile.pk).update(openid='different-account')
        adapter = RecordingAdapter(self)
        with self.assertRaises(ValidationError): spend.execute(a.pk, adapter=adapter, apply=fulfill)
        a.refresh_from_db()
        self.assertEqual((a.status, adapter.calls), ('failed', []))

    def test_status_revalidated_after_prepare(self):
        from .order_spend import prepare, fulfill
        a = prepare(self.order.order_no, self.user)
        Order.objects.filter(pk=self.order.pk).update(status=Order.STATUS_WAITING)
        adapter = RecordingAdapter(self)
        with self.assertRaises(ValidationError): spend.execute(a.pk, adapter=adapter, apply=fulfill)
        a.refresh_from_db()
        self.assertEqual((a.status, adapter.calls), ('failed', []))

    def test_query_route_completes_capture_when_platform_closed(self):
        from rest_framework.test import APIClient
        with patch('apps.wallet.order_spend.fulfill', side_effect=RuntimeError('local fail')):
            with self.assertRaises(RuntimeError): self.runpay()
        client = APIClient(); client.force_authenticate(self.user)
        with override_settings(SHARED_SPEND_PLATFORM_APPROVED=False, WECHAT_VIRTUALPAY_ENABLED=False), \
             patch('apps.wallet.spend_adapter.user_xpay_post') as remote:
            response = client.post('/api/pay/wechat/virtual/query-order/' + self.order.order_no, {})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], 'paid')
        remote.assert_not_called()

    def test_ios_net_credit_original_capture_never_spends_gross_recharge(self):
        ClientWalletLedger.objects.filter(reference_id='DURABLE-SOURCE').update(amount=Decimal('7'))
        ClientWalletLedger.objects.create(wallet=self.wallet, entry_type='admin_adjust',
            amount=Decimal('3'), balance_after=Decimal('10'))
        result = self.runpay()
        self.assertEqual(result['wechat_coin_units'], 60)
        a = WalletSpendAttempt.objects.get()
        self.assertEqual(a.source_snapshot['allocations'][0]['source_credit_amount'], '7.00')
        self.assertEqual(a.source_snapshot['allocations'][0]['source_paid_amount'], '10.00')

    def test_old_balance_api_does_not_report_unknown_as_success(self):
        from rest_framework.test import APIClient
        client = APIClient(); client.force_authenticate(self.user)
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=RecordingAdapter(self, timeout=True)):
            response = client.post('/api/pay/balance/create', {'order_no': self.order.order_no, 'code': 'fake'}, format='json')
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(str(response.data['code']), 'ORDER_PAYMENT_PENDING')
        self.assertEqual(response.data['status'], 'unknown')
        self.order.refresh_from_db()
        self.assertFalse(self.order.paid)

    def test_pending_capture_blocks_new_payment_record(self):
        from .order_spend import prepare
        prepare(self.order.order_no, self.user)
        with self.assertRaises(ValidationError):
            Payment.objects.create(order=self.order, payment_no='RACING-CASH', amount=6,
                channel='wechat', scene='jsapi', status='paying')
        self.assertFalse(Payment.objects.filter(order=self.order).exists())

    def test_cancel_local_failure_no_remote_refund_and_retry_conserves_source(self):
        from rest_framework.test import APIClient
        from apps.payments.models import Refund
        self.runpay()
        Order.objects.filter(pk=self.order.pk).update(status=Order.STATUS_WAITING)
        client = APIClient(); client.force_authenticate(self.user)
        url = '/api/boss/order/' + self.order.order_no + '/cancel'
        with patch('apps.wallet.coin_sync.user_xpay_post') as remote, \
             patch('apps.wallet.services.write_wallet_ledger', side_effect=RuntimeError('local refund fail')):
            with self.assertRaises(RuntimeError): client.post(url, {}, format='json')
        remote.assert_not_called()
        self.order.refresh_from_db(); self.wallet.refresh_from_db()
        self.assertEqual((self.order.status, self.wallet.balance), (Order.STATUS_WAITING, Decimal('4')))
        self.assertEqual(Refund.objects.count(), 0)
        with patch('apps.wallet.coin_sync.user_xpay_post') as remote:
            response = client.post(url, {}, format='json')
            self.assertEqual(response.status_code, 200, response.data)
            client.post(url, {}, format='json')
        remote.assert_not_called()
        self.assertEqual(Refund.objects.count(), 1)
        refund = Refund.objects.get()
        self.assertEqual(refund.notify_payload.get('wechat_coin_refund_units'), 60)
        self.assertEqual(refund.notify_payload.get('wechat_coin_refund_status'), 'pending')
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('10'))
