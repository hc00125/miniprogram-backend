"""Lifecycle integration tests; synthetic settings only, no production adapters."""
from decimal import Decimal
from datetime import timedelta
from django.test import TransactionTestCase, override_settings
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient
from django.contrib.auth.models import User
from apps.accounts.models import ClientProfile
from apps.catalog.models import Package, PlayerType
from apps.players.models import Player
from apps.orders.models import Order
from apps.orders.checkout_payment import pay
from apps.orders.services import grab_order, start_timer, complete_order
from apps.earnings.models import EarningsConfig, PlayerEarning, PlayerWallet, OrderCommissionOverride
from apps.earnings.services import create_order_earnings
from apps.wallet.models import ClientWallet


@override_settings(ORDER_SURCHARGE_ENABLED=True)
class SurchargeLifecycleTests(TransactionTestCase):
    def setUp(self):
        self.assertIn(connection.settings_dict['ENGINE'], ('django.db.backends.sqlite3', 'django.db.backends.postgresql'))
        if connection.vendor == 'postgresql':
            self.assertEqual(connection.settings_dict['NAME'], 'test_touchi_backend_v2')
            self.assertEqual(connection.settings_dict['PORT'], '55459')
            self.assertIn('/backend-pg', connection.settings_dict['HOST'])
        self.user = User.objects.create_user('lifecycle-boss')
        self.profile = ClientProfile.objects.create(user=self.user, openid='offline-lifecycle')
        self.wallet, _ = ClientWallet.objects.update_or_create(profile=self.profile, defaults={'balance': Decimal('50')})
        self.package = Package.objects.create(name='lifecycle', base_price=10, player_count=3)
        self.config, _ = EarningsConfig.objects.update_or_create(key='default', defaults={
            'default_commission_rate': Decimal('16'), 'review_days': 5, 'min_withdrawal_amount': Decimal('.1')})
        kind = PlayerType.objects.create(name='lifecycle-type', priority=1)
        self.players = [Player.objects.create(name='lifecycle-'+str(i), player_type=kind, is_online=True) for i in range(3)]
        self.client = APIClient(); self.client.force_authenticate(self.user)
        data = {'boss_wechat': 'offline', 'package_id': self.package.pk, 'surcharge_diamonds': 12}
        quote = self.client.post('/api/boss/order/quote', data, format='json')
        self.assertEqual(quote.status_code, 200, quote.data)
        data.update(quote_version=quote.data['quote_version'], idempotency_key='lifecycle')
        response = self.client.post('/api/boss/order', data, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.order = Order.objects.get(order_no=response.data['order_no'])

    def finish(self):
        pay(self.order.order_no, self.user)
        for player in self.players:
            self.order = grab_order(self.order.order_no, player)
        self.assertEqual(self.order.status, Order.STATUS_READY_TO_START)
        self.order = start_timer(self.order, self.players[0])
        for player in self.players:
            self.order = complete_order(self.order, player)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_COMPLETED)

    def add_coin(self):
        from apps.wallet.models import RechargeOrder, ClientWalletLedger
        RechargeOrder.objects.create(recharge_no='lifecycle-coin', profile=self.profile,
            amount=Decimal('50'), status='credited', notify_payload={
                'mode': 'short_series_coin', 'wechat_coin_units_per_yuan': 10})
        ClientWalletLedger.objects.create(wallet=self.wallet, entry_type='recharge',
            amount=Decimal('50'), balance_after=Decimal('50'), reference_id='lifecycle-coin')

    def test_coin_cancel_durable_intent_no_remote_in_cancel_and_source_restored_once(self):
        from unittest.mock import patch
        from apps.wallet.coin_sync import has_pending_coin_refunds, coin_backed_wallet_amount, sync_pending_coin_refunds
        from apps.orders.surcharge_models import OrderCheckoutRefund
        from apps.wallet import spend_service
        class Spend:
            calls = 0
            def authenticate(self, attempt, code): return 'offline'
            def spend(self, attempt, session):
                self.calls += 1
                return {'errcode': 0, 'order_id': attempt.external_id}
        self.add_coin(); adapter = Spend()
        pay(self.order.order_no, self.user, adapter=adapter)
        with patch('apps.wallet.coin_sync.user_xpay_post') as external:
            response = self.client.post('/api/boss/order/'+self.order.order_no+'/cancel', {}, format='json')
            self.assertEqual(response.status_code, 200, response.data)
            external.assert_not_called()
        self.assertTrue(has_pending_coin_refunds(self.profile))
        intent = OrderCheckoutRefund.objects.get()
        self.assertEqual(intent.remote_status, 'prepared')
        self.assertEqual(intent.request_payload['amount'], 112)
        self.assertEqual(coin_backed_wallet_amount(self.profile), Decimal('38.80'))
        observed = []
        def remote(path, payload, session, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            intent.refresh_from_db()
            self.assertEqual(intent.remote_status, 'dispatching')
            self.assertEqual(payload, intent.request_payload)
            observed.append(payload)
            return {'errcode': 0}
        with patch('apps.wallet.coin_sync.user_xpay_post', side_effect=remote):
            sync_pending_coin_refunds(self.profile, 'offline')
            sync_pending_coin_refunds(self.profile, 'offline')
        self.assertEqual(len(observed), 1)
        self.assertFalse(has_pending_coin_refunds(self.profile))
        self.assertEqual(coin_backed_wallet_amount(self.profile), Decimal('50'))
        self.assertEqual(adapter.calls, 1)

    def test_prepared_never_dispatched_cancels_without_money_or_remote(self):
        from apps.orders.checkout_payment import prepare
        from apps.wallet.models import WalletSpendAttempt
        item = prepare(self.order.order_no, self.user)
        response = self.client.post('/api/boss/order/'+self.order.order_no+'/cancel', {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        attempt = WalletSpendAttempt.objects.get(pk=item.attempt_id)
        self.assertEqual((attempt.status, attempt.reserved_amount), ('failed', Decimal('0')))
        self.order.refresh_from_db(); self.wallet.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_CANCELLED)
        self.assertEqual(self.wallet.balance, Decimal('50'))

    def test_coin_paid_completion_uses_original_spend_source_once(self):
        from apps.orders.surcharge_models import OrderCheckout
        self.add_coin()
        class Spend:
            calls = 0
            def authenticate(self, attempt, code): return 'offline'
            def spend(self, attempt, session):
                self.calls += 1
                return {'errcode': 0, 'order_id': attempt.external_id}
        adapter = Spend()
        pay(self.order.order_no, self.user, adapter=adapter)
        self.finish()
        attempt = OrderCheckout.objects.get(order=self.order).attempt
        bonus = PlayerEarning.objects.filter(order=self.order, source='surcharge')
        self.assertEqual(sum(e.net_amount for e in bonus), Decimal('9'))
        self.assertEqual(attempt.source_snapshot['coin_units'], 112)
        for earning in bonus:
            self.assertEqual(earning.source_snapshot['attempt_id'], attempt.pk)
            self.assertEqual(earning.source_snapshot['external_id'], attempt.external_id)
        self.assertEqual(adapter.calls, 1)

    def test_mixed_coin_cash_refund_preserves_subunit_cash_exactly(self):
        from apps.wallet.models import RechargeOrder, ClientWalletLedger
        from apps.wallet.coin_sync import sync_pending_coin_refunds, coin_backed_wallet_amount
        from apps.orders.surcharge_models import OrderCheckoutRefund
        from unittest.mock import patch
        self.package.base_price = Decimal('10.05'); self.package.save(update_fields=['base_price'])
        data = {'boss_wechat': 'offline', 'package_id': self.package.pk, 'surcharge_diamonds': 12}
        quote = self.client.post('/api/boss/order/quote', data, format='json').data
        data.update(quote_version=quote['quote_version'], idempotency_key='mixed-precision')
        response = self.client.post('/api/boss/order', data, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        order = Order.objects.get(order_no=response.data['order_no'])
        RechargeOrder.objects.create(recharge_no='mixed-coin', profile=self.profile, amount=Decimal('11.20'),
            status='credited', notify_payload={'mode': 'short_series_coin', 'wechat_coin_units_per_yuan': 10})
        ClientWalletLedger.objects.create(wallet=self.wallet, entry_type='recharge', amount=Decimal('11.20'),
            balance_after=Decimal('11.20'), reference_id='mixed-coin')
        ClientWalletLedger.objects.create(wallet=self.wallet, entry_type='admin_adjust', amount=Decimal('38.80'),
            balance_after=Decimal('50'), reference_id='offline-local')
        class Spend:
            def authenticate(self, attempt, code): return 'offline'
            def spend(self, attempt, session): return {'errcode': 0, 'order_id': attempt.external_id}
        pay(order.order_no, self.user, adapter=Spend())
        response = self.client.post('/api/boss/order/'+order.order_no+'/cancel', {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        with patch('apps.wallet.coin_sync.user_xpay_post', return_value={'errcode': 0}) as remote:
            sync_pending_coin_refunds(self.profile, 'offline')
            self.assertEqual(remote.call_count, 1)
        self.assertEqual(coin_backed_wallet_amount(self.profile), Decimal('11.20'))
        intent = OrderCheckoutRefund.objects.get()
        self.assertEqual(Decimal(intent.source_snapshot['base_coin_yuan']) * 10,
                         (Decimal(intent.source_snapshot['base_coin_yuan']) * 10).to_integral_value())
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('50'))

    def test_refund_dto_does_not_misreport_refunded_checkout_as_new_payment(self):
        pay(self.order.order_no, self.user)
        response = self.client.post('/api/boss/order/'+self.order.order_no+'/cancel', {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        response = self.client.get('/api/boss/order/by-key', {'idempotency_key': 'lifecycle'})
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data['checkout']
        self.assertEqual(data['payment_status'], 'paid')  # backward compatible capture status
        self.assertEqual(data['refund']['local_status'], 'succeeded')
        self.assertEqual(data['refund']['base_amount_yuan'], '10.00')
        self.assertEqual(data['refund']['surcharge_amount_yuan'], '1.20')
        self.assertEqual(data['refund']['total_amount_yuan'], '11.20')
        self.assertEqual(data['refund']['remote_status'], 'not_required')

    def test_refund_failure_rolls_back_original_surcharge_sources_and_status(self):
        from unittest.mock import patch
        from apps.orders.surcharge_models import OrderCheckoutRefund
        from apps.payments.models import Payment, Refund
        from apps.wallet.models import ClientWalletLedger
        pay(self.order.order_no, self.user)
        original = Order.save
        def fail_cancel(order, *args, **kwargs):
            if order.status == Order.STATUS_CANCELLED:
                raise RuntimeError('offline final write fault')
            return original(order, *args, **kwargs)
        with patch.object(Order, 'save', fail_cancel), self.assertRaises(RuntimeError):
            self.client.post('/api/boss/order/'+self.order.order_no+'/cancel', {}, format='json')
        self.order.refresh_from_db(); self.wallet.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_WAITING)
        self.assertEqual(self.wallet.balance, Decimal('38.80'))
        self.assertEqual(Payment.objects.get(order=self.order).status, 'paid')
        self.assertEqual(self.order.surcharges.get().status, 'paid')
        self.assertFalse(Refund.objects.exists()); self.assertFalse(OrderCheckoutRefund.objects.exists())
        self.assertFalse(ClientWalletLedger.objects.filter(entry_type='refund_in').exists())

    def test_unknown_refund_never_resends_or_releases_backing(self):
        from unittest.mock import patch
        from apps.wallet.coin_sync import sync_pending_coin_refunds, has_pending_coin_refunds, coin_backed_wallet_amount
        from apps.orders.surcharge_models import OrderCheckoutRefund
        class Spend:
            def authenticate(self, attempt, code): return 'offline'
            def spend(self, attempt, session): return {'errcode': 0, 'order_id': attempt.external_id}
        self.add_coin()
        pay(self.order.order_no, self.user, adapter=Spend())
        response = self.client.post('/api/boss/order/'+self.order.order_no+'/cancel', {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        intent = OrderCheckoutRefund.objects.get()
        original = (intent.refund_no, intent.request_payload, intent.source_snapshot)
        with patch('apps.wallet.coin_sync.user_xpay_post', side_effect=TimeoutError('lost refund reply')) as remote:
            for _ in range(3): sync_pending_coin_refunds(self.profile, 'offline')
            self.assertEqual(remote.call_count, 1)
        intent.refresh_from_db()
        self.assertEqual(intent.remote_status, 'unknown')
        self.assertEqual(original, (intent.refund_no, intent.request_payload, intent.source_snapshot))
        self.assertTrue(has_pending_coin_refunds(self.profile))
        self.assertEqual(coin_backed_wallet_amount(self.profile), Decimal('38.80'))
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('50'))

    def test_source_dto_freeze_release_and_real_withdrawal_return_failure_paid(self):
        from apps.earnings.services import (freeze_pending_earning, unfreeze_earning, release_due_earnings,
            create_withdrawal, approve_withdrawal, return_withdrawal, mark_withdrawal_paid)
        from apps.earnings.models import Withdrawal, WithdrawalAllocation
        from apps.earnings.serializers import PlayerEarningSerializer
        from rest_framework.exceptions import ValidationError
        from unittest.mock import patch
        self.finish()
        player = self.players[0]
        bonus = PlayerEarning.objects.get(order=self.order, player=player, source='surcharge')
        dto = PlayerEarningSerializer(bonus).data
        self.assertEqual(dto['source'], 'surcharge')
        self.assertEqual(dto['surcharge_no'], self.order.surcharges.get().surcharge_no)
        original_deadline = bonus.review_until
        freeze_pending_earning(bonus, 'offline review')
        self.config.review_days = 99; self.config.save()
        release_due_earnings(now=original_deadline + timedelta(seconds=1), player=player)
        bonus.refresh_from_db(); self.assertEqual(bonus.status, 'frozen')
        self.assertEqual(bonus.review_until, original_deadline)
        with patch('apps.earnings.settlements.timezone.now', return_value=original_deadline + timedelta(seconds=1)):
            unfreeze_earning(bonus)
        bonus.refresh_from_db(); self.assertEqual(bonus.available_amount, Decimal('3'))
        wallet = PlayerWallet.objects.get(player=player)
        total = wallet.available_balance
        for failed in (False, True):
            withdrawal = create_withdrawal(player, total, 'wechat', 'offline', 'offline-account')
            self.assertEqual(sum(a.amount for a in withdrawal.allocations.all()), total)
            allocation = withdrawal.allocations.get(earning=bonus)
            self.assertEqual(allocation.amount, Decimal('3'))
            approve_withdrawal(withdrawal)
            return_withdrawal(withdrawal, 'offline failure', failed=failed)
            with self.assertRaises(ValidationError): return_withdrawal(withdrawal, 'repeat')
        withdrawal = create_withdrawal(player, total, 'wechat', 'offline', 'offline-account')
        approve_withdrawal(withdrawal)
        withdrawal.transfer_no = 'offline-proof'; withdrawal.save(update_fields=['transfer_no'])
        mark_withdrawal_paid(withdrawal)
        with self.assertRaises(ValidationError): mark_withdrawal_paid(withdrawal)
        bonus.refresh_from_db(); self.assertEqual(bonus.withdrawn_amount, Decimal('3'))
        self.assertEqual(bonus.available_amount + bonus.withdrawing_amount + bonus.withdrawn_amount,
                         bonus.net_amount - bonus.reversed_amount - bonus.debt_offset_amount)

    def test_config_recalculation_cannot_change_surcharge_snapshot(self):
        from apps.earnings.services import recalculate_pending_order_earnings
        self.finish()
        before = list(PlayerEarning.objects.filter(order=self.order, source='surcharge').order_by('pk').values())
        OrderCommissionOverride.objects.update_or_create(order=self.order,
            defaults={'commission_rate': 80, 'reason': 'base only'})
        self.config.review_days = 99; self.config.save()
        recalculate_pending_order_earnings(self.order)
        create_order_earnings(self.order)
        after = list(PlayerEarning.objects.filter(order=self.order, source='surcharge').order_by('pk').values())
        self.assertEqual(before, after)
        self.assertTrue(all(e.commission_rate == 80 for e in PlayerEarning.objects.filter(order=self.order, source='order')))

    def test_paid_waiting_cancel_refunds_base_and_surcharge_once(self):
        from apps.payments.models import Refund
        from apps.wallet.models import ClientWalletLedger
        pay(self.order.order_no, self.user)
        for _ in range(2):
            response = self.client.post('/api/boss/order/'+self.order.order_no+'/cancel', {}, format='json')
            self.assertEqual(response.status_code, 200, response.data)
        self.wallet.refresh_from_db(); self.order.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('50'))
        self.assertEqual(self.order.status, Order.STATUS_CANCELLED)
        self.assertEqual(self.order.surcharges.get().status, 'refunded')
        self.assertEqual(Refund.objects.get(order=self.order).amount, Decimal('10'))
        credits = ClientWalletLedger.objects.filter(wallet=self.wallet, entry_type='refund_in')
        self.assertEqual(sum(e.amount for e in credits), Decimal('11.20'))

    def test_paid_full_lineup_completes_with_independent_fixed25_income_once(self):
        self.finish()
        earnings = list(PlayerEarning.objects.filter(order=self.order))
        self.assertEqual(len(earnings), 6)
        bonus = list(PlayerEarning.objects.filter(order=self.order, source='surcharge'))
        base = list(PlayerEarning.objects.filter(order=self.order, source='order'))
        self.assertEqual(len(bonus), 3)
        self.assertEqual(sum(e.gross_amount for e in base), Decimal('100'))
        for earning in bonus:
            self.assertEqual((earning.gross_amount, earning.commission_amount, earning.net_amount),
                             (Decimal('4'), Decimal('1'), Decimal('3')))
            self.assertEqual(earning.commission_rate, Decimal('25'))
            self.assertEqual(earning.review_until, self.order.end_time + timedelta(days=5))
            self.assertEqual(earning.surcharge_id, self.order.surcharges.get().pk)
        self.assertEqual(sum(e.commission_amount + e.net_amount for e in bonus), Decimal('12'))
        create_order_earnings(self.order)
        self.assertEqual(PlayerEarning.objects.filter(order=self.order).count(), 6)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('38.80'))
        self.assertEqual(Decimal(str(self.order.total_amount)), Decimal('10'))
