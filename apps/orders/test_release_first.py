"""Release-first: production gates with offline, network-denied fixtures."""
from decimal import Decimal
from unittest.mock import patch
from django.conf import settings
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient
from django.contrib.auth.models import User
from apps.accounts.models import ClientProfile
from apps.catalog.models import Package, PlayerType
from apps.players.models import Player
from apps.orders.models import Order
from apps.orders.surcharge_models import OrderCheckout
from apps.wallet.models import ClientWallet, ClientWalletLedger, RechargeOrder, WalletSpendAttempt


class OfflineAdapter:
    def __init__(self, auth_hook=None, outcome='success'):
        self.auth_hook, self.outcome = auth_hook, outcome
        self.auth_calls = self.spend_calls = 0

    def authenticate(self, attempt, code):
        self.auth_calls += 1
        if self.auth_hook:
            self.auth_hook()
        return 'offline-session'

    def spend(self, attempt, session):
        self.spend_calls += 1
        if self.outcome == 'unknown':
            raise TimeoutError('offline response lost')
        return {'errcode': 0 if self.outcome == 'success' else 999,
            'order_id': attempt.external_id}


@override_settings(ORDER_SURCHARGE_ISOLATED_TEST_MODE=False,
    ORDER_SURCHARGE_CHECKOUT_READY=True, ORDER_SURCHARGE_ENABLED=True,
    SHARED_SPEND_PLATFORM_APPROVED=True, WECHAT_VIRTUALPAY_ENABLED=True,
    GIFT_PURCHASE_ENABLED=False, GIFT_TRANSACTION_POLICY={})
class ReleaseFirstTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('release-first-boss')
        self.profile = ClientProfile.objects.create(user=self.user, openid='offline-release-first')
        self.wallet, _ = ClientWallet.objects.update_or_create(profile=self.profile,
            defaults={'balance': Decimal('50')})
        self.package = Package.objects.create(name='release-first', base_price=10, player_count=3)
        kind = PlayerType.objects.create(name='release-first', priority=1)
        self.players = [Player.objects.create(name='release-first-'+str(i), player_type=kind, is_online=True)
            for i in range(3)]
        self.client = APIClient(); self.client.force_authenticate(self.user)

    def create(self, key='release-first'):
        data = {'boss_wechat': 'offline', 'package_id': self.package.pk, 'surcharge_diamonds': 12}
        quote = self.client.post('/api/boss/order/quote', data, format='json')
        self.assertEqual(quote.status_code, 200, quote.data)
        self.assertTrue(quote.data['can_submit'], quote.data)
        self.assertEqual(quote.data['blockers'], [])
        data.update(quote_version=quote.data['quote_version'], idempotency_key=key)
        response = self.client.post('/api/boss/order', data, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['checkout']['blockers'], [])
        return Order.objects.get(order_no=response.data['order_no'])

    def coin(self):
        RechargeOrder.objects.create(recharge_no='release-first-coin', profile=self.profile,
            amount=Decimal('50'), status='credited', notify_payload={
                'mode': 'short_series_coin', 'wechat_coin_units_per_yuan': 10})
        ClientWalletLedger.objects.create(wallet=self.wallet, entry_type='recharge',
            amount=Decimal('50'), balance_after=Decimal('50'), reference_id='release-first-coin')

    def pay(self, order, adapter=None):
        from apps.orders.checkout_payment import pay
        return pay(order.order_no, self.user, adapter=adapter)

    def finish(self, order):
        from apps.orders.services import grab_order, start_timer, complete_order
        for player in self.players:
            order = grab_order(order.order_no, player)
        order = start_timer(order, self.players[0])
        for player in self.players:
            order = complete_order(order, player)
        return order

    def test_formal_gates_quote_payment_completion_and_cancel_without_gift_policy(self):
        from apps.earnings.models import PlayerEarning
        from apps.payments.models import Payment
        from apps.earnings.serializers import PlayerEarningSerializer
        self.assertFalse(settings.ORDER_SURCHARGE_ISOLATED_TEST_MODE)
        self.assertEqual(settings.GIFT_TRANSACTION_POLICY, {})
        first = self.create()
        response = self.client.post('/api/pay/balance/create', {'order_no': first.order_no}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['amount'], '11.20')
        self.assertEqual(response.data['checkout']['blockers'], [])
        self.finish(first)
        self.assertEqual(PlayerEarning.objects.filter(order=first, source='surcharge').count(), 3)
        bonus = PlayerEarning.objects.get(order=first, player=self.players[0], source='surcharge')
        self.assertEqual((bonus.gross_amount, bonus.commission_amount, bonus.net_amount),
            (Decimal('4'), Decimal('1'), Decimal('3')))
        dto = PlayerEarningSerializer(bonus).data
        self.assertEqual(dto['source'], 'surcharge')
        self.assertEqual(dto['surcharge_no'], first.surcharges.get().surcharge_no)
        second = self.create('cancel')
        self.pay(second)
        for _ in range(2):
            response = self.client.post('/api/boss/order/'+second.order_no+'/cancel', {}, format='json')
            self.assertEqual(response.status_code, 200, response.data)
        dto = self.client.get('/api/boss/order/by-key', {'idempotency_key': 'cancel'}).data['checkout']
        self.assertEqual(dto['payment_status'], 'paid')
        self.assertEqual(dto['refund']['total_amount_yuan'], '11.20')
        self.assertEqual(dto['refund']['remote_status'], 'not_required')
        self.assertEqual(Payment.objects.get(order=first).amount, Decimal('10'))
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('38.80'))

    def test_closed_gates_block_quote_create_and_payment_before_any_adapter_call(self):
        from rest_framework.exceptions import ValidationError
        from apps.orders.checkout_payment import prepare
        from apps.orders.checkout import checkout_data
        self.coin()
        for index, (flag, code) in enumerate((
            ('ORDER_SURCHARGE_CHECKOUT_READY', 'SURCHARGE_LIFECYCLE_UNAVAILABLE'),
            ('ORDER_SURCHARGE_ENABLED', 'FEATURE_DISABLED'),
            ('SHARED_SPEND_PLATFORM_APPROVED', 'POLICY_UNCONFIRMED'))):
            order = self.create('closed-'+str(index))
            for prepared in (False, True):
                item = prepare(order.order_no, self.user) if prepared else None
                adapter = OfflineAdapter()
                with self.subTest(flag=flag, prepared=prepared), override_settings(**{flag: False}):
                    data = {'boss_wechat': 'offline', 'package_id': self.package.pk, 'surcharge_diamonds': 12}
                    quote = self.client.post('/api/boss/order/quote', data, format='json').data
                    self.assertFalse(quote['can_submit'])
                    self.assertIn(code, quote['blockers'])
                    self.assertIn(code, checkout_data(order)['blockers'])
                    data.update(quote_version=quote['quote_version'], idempotency_key='denied-'+str(index))
                    response = self.client.post('/api/boss/order', data, format='json')
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertEqual(response.data['code'], code)
                    with self.assertRaises(ValidationError) as caught:
                        self.pay(order, adapter)
                    self.assertEqual(str(caught.exception.detail['code']), code)
                    self.assertEqual((adapter.auth_calls, adapter.spend_calls), (0, 0))
                if item:
                    item.attempt.refresh_from_db()
                    self.assertEqual((item.attempt.status, item.attempt.reserved_amount), ('failed', Decimal('0')))
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('50'))
        self.assertFalse(ClientWalletLedger.objects.filter(entry_type='shared_spend').exists())

    def test_succeeded_capture_recovers_after_account_and_flags_revoked_without_auth(self):
        from apps.payments.models import Payment
        self.coin(); order = self.create()
        adapter = OfflineAdapter()
        with patch('apps.orders.checkout_payment.fulfill', side_effect=RuntimeError('local fault')):
            with self.assertRaises(RuntimeError):
                self.pay(order, adapter)
        attempt = WalletSpendAttempt.objects.get()
        self.assertEqual(attempt.status, 'succeeded')
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        ClientProfile.objects.filter(pk=self.profile.pk).update(account_status='banned')
        with override_settings(ORDER_SURCHARGE_CHECKOUT_READY=False, ORDER_SURCHARGE_ENABLED=False,
                SHARED_SPEND_PLATFORM_APPROVED=False, WECHAT_VIRTUALPAY_ENABLED=False):
            for _ in range(2):
                self.assertEqual(self.pay(order, adapter)['status'], 'paid')
        self.assertEqual((adapter.auth_calls, adapter.spend_calls), (1, 1))
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('38.80'))
        self.assertEqual(Payment.objects.filter(order=order).count(), 1)
        self.assertEqual(ClientWalletLedger.objects.filter(entry_type='shared_spend').count(), 1)

    def test_first_dispatch_rechecks_identity_changed_during_authentication(self):
        from rest_framework.exceptions import ValidationError
        self.coin(); order = self.create()
        adapter = OfflineAdapter(auth_hook=lambda: ClientProfile.objects.filter(pk=self.profile.pk).update(openid='changed'))
        with self.assertRaises(ValidationError) as caught:
            self.pay(order, adapter)
        self.assertEqual(str(caught.exception.detail['code']), 'IDENTITY_CHANGED')
        self.assertEqual(adapter.spend_calls, 0)
        attempt = WalletSpendAttempt.objects.get()
        self.assertEqual((attempt.status, attempt.reserved_amount), ('failed', Decimal('0')))

    def test_first_dispatch_rechecks_account_and_platform_after_authentication(self):
        from rest_framework.exceptions import ValidationError
        self.coin()
        for index, mode in enumerate(('account', 'platform')):
            order = self.create('revoked-'+str(index))
            def revoke():
                if mode == 'account':
                    ClientProfile.objects.filter(pk=self.profile.pk).update(account_status='banned')
                else:
                    settings.SHARED_SPEND_PLATFORM_APPROVED = False
            adapter = OfflineAdapter(auth_hook=revoke)
            try:
                with self.assertRaises(ValidationError):
                    self.pay(order, adapter)
                self.assertEqual(adapter.spend_calls, 0)
                self.assertEqual(OrderCheckout.objects.get(order=order).attempt.status, 'failed')
            finally:
                ClientProfile.objects.filter(pk=self.profile.pk).update(account_status='active')
                settings.SHARED_SPEND_PLATFORM_APPROVED = True

    def test_complex_refunds_are_explicit_customer_service_without_money_or_eligibility_changes(self):
        from apps.orders.services import grab_order, start_timer, complete_order
        from apps.payments.models import Payment, Refund
        from apps.payments.refund_integrity import ensure_payment_refund
        from apps.earnings.models import PlayerEarning
        from rest_framework.exceptions import ValidationError
        order = self.create(); self.pay(order)
        payment = Payment.objects.get(order=order)
        for stage in ('waiting', 'ready', 'in_progress', 'completed'):
            if stage == 'ready':
                for player in self.players: order = grab_order(order.order_no, player)
            elif stage == 'in_progress':
                order = start_timer(order, self.players[0])
            elif stage == 'completed':
                for player in self.players: order = complete_order(order, player)
            before = list(PlayerEarning.objects.filter(order=order).order_by('pk').values())
            with self.subTest(stage=stage), patch('apps.wallet.coin_sync.user_xpay_post') as remote:
                for amount in (Decimal('5'), Decimal('10')):
                    with self.assertRaises(ValidationError) as caught:
                        ensure_payment_refund(payment.payment_no, amount)
                    self.assertEqual(str(caught.exception.detail['code']), 'SURCHARGE_REFUND_REQUIRES_REVIEW')
                    self.assertEqual(str(caught.exception.detail['detail']), '加价订单此类售后请联系客服处理')
                if stage != 'waiting':
                    status = order.status
                    response = self.client.post('/api/boss/order/'+order.order_no+'/cancel', {}, format='json')
                    self.assertEqual(response.status_code, 409, response.data)
                    self.assertEqual(str(response.data['code']), 'SURCHARGE_REFUND_REQUIRES_REVIEW')
                    self.assertEqual(str(response.data['detail']), '加价订单此类售后请联系客服处理')
                    order.refresh_from_db(); self.assertEqual(order.status, status)
                remote.assert_not_called()
                self.assertFalse(Refund.objects.exists())
                self.assertFalse(ClientWalletLedger.objects.filter(entry_type='refund_in').exists())
                self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('38.80'))
                self.assertEqual(before, list(PlayerEarning.objects.filter(order=order).order_by('pk').values()))

    def test_formal_ready_never_reopens_standalone_surcharge(self):
        from apps.orders.surcharge_payment import prepare
        from rest_framework.exceptions import ValidationError
        order = self.create()
        with self.assertRaises(ValidationError) as caught:
            prepare(order.order_no, self.user, amount_diamonds=12, idempotency_key='standalone')
        self.assertEqual(str(caught.exception.detail['code']), 'SURCHARGE_PREORDER_ONLY')
        self.assertEqual(order.surcharges.count(), 1)
        self.assertFalse(WalletSpendAttempt.objects.exists())

    def test_formal_coin_payment_completion_and_refund_sources_are_exact(self):
        from apps.wallet.coin_sync import coin_backed_wallet_amount, has_pending_coin_refunds, sync_pending_coin_refunds
        from apps.earnings.models import PlayerEarning
        self.coin(); first = self.create()
        adapter = OfflineAdapter()
        self.assertEqual(self.pay(first, adapter)['status'], 'paid')
        self.finish(first)
        attempt = OrderCheckout.objects.get(order=first).attempt
        self.assertEqual((attempt.amount, attempt.source_snapshot['coin_units']), (Decimal('11.20'), 112))
        self.assertEqual(Decimal(attempt.source_snapshot['local_amount']), Decimal('0'))
        self.assertEqual(attempt.source_snapshot['allocations'][0]['reference_id'], 'release-first-coin')
        self.assertTrue(all(e.source_snapshot['external_id'] == attempt.external_id
            for e in PlayerEarning.objects.filter(order=first, source='surcharge')))
        second = self.create('coin-cancel'); self.pay(second, adapter)
        with patch('apps.wallet.coin_sync.user_xpay_post') as remote:
            response = self.client.post('/api/boss/order/'+second.order_no+'/cancel', {}, format='json')
            self.assertEqual(response.status_code, 200, response.data)
            remote.assert_not_called()
        self.assertTrue(has_pending_coin_refunds(self.profile))
        self.assertEqual(coin_backed_wallet_amount(self.profile), Decimal('27.60'))
        with patch('apps.wallet.coin_sync.user_xpay_post', return_value={'errcode': 0}) as remote:
            sync_pending_coin_refunds(self.profile, 'offline')
            sync_pending_coin_refunds(self.profile, 'offline')
            self.assertEqual(remote.call_count, 1)
            self.assertEqual(remote.call_args.args[1]['amount'], 112)
        self.assertEqual(coin_backed_wallet_amount(self.profile), Decimal('38.80'))
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('38.80'))
        self.assertEqual(adapter.spend_calls, 2)

    def test_coin_switch_closed_even_prepared_has_zero_auth_or_spend(self):
        from apps.orders.checkout_payment import prepare
        from rest_framework.exceptions import ValidationError
        self.coin(); order = self.create(); item = prepare(order.order_no, self.user)
        adapter = OfflineAdapter()
        with override_settings(WECHAT_VIRTUALPAY_ENABLED=False), self.assertRaises(ValidationError):
            self.pay(order, adapter)
        self.assertEqual((adapter.auth_calls, adapter.spend_calls), (0, 0))
        item.attempt.refresh_from_db()
        self.assertEqual((item.attempt.status, item.attempt.reserved_amount), ('failed', Decimal('0')))

    def test_false_ready_is_not_bypassed_by_test_flag(self):
        with override_settings(ORDER_SURCHARGE_CHECKOUT_READY=False, ORDER_SURCHARGE_ISOLATED_TEST_MODE=True):
            response = self.client.post('/api/boss/order/quote', {'boss_wechat': 'offline',
                'package_id': self.package.pk, 'surcharge_diamonds': 12}, format='json')
        self.assertFalse(response.data['can_submit'])
        self.assertIn('SURCHARGE_LIFECYCLE_UNAVAILABLE', response.data['blockers'])
