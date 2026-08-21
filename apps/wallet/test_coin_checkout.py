from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order
from apps.payments.models import Payment, Refund

from .coin_balance_service import pay_order_with_coin_aware_balance
from .models import ClientWallet, ClientWalletLedger, RechargeOrder


VIRTUAL_SETTINGS = {
    'WECHAT_VIRTUALPAY_ENABLED': True,
    'WECHAT_APP_ID': 'wx_test_appid',
    'WECHAT_APP_SECRET': 'test_secret',
    'WECHAT_VIRTUALPAY_OFFER_ID': 'test_offer',
    'WECHAT_VIRTUALPAY_APP_KEY': 'test_app_key',
    'WECHAT_VIRTUALPAY_ENV': 0,
    'WECHAT_VIRTUALPAY_HTTP_TIMEOUT': 10,
    'WECHAT_VIRTUALPAY_COIN_UNITS_PER_YUAN': 100,
    'ENABLE_MOCK_PAYMENT': False,
}


@override_settings(**VIRTUAL_SETTINGS)
class CoinBackedBalanceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='coin-balance-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='openid_coin_balance',
            nickname='官方钻石余额测试',
        )
        self.package = Package.objects.create(name='Coin支付套餐', player_count=1, base_price=30)
        self.order = Order.objects.create(
            order_no='COIN_BALANCE_ORDER_1',
            boss_user=self.user,
            boss_wechat='coin-boss',
            package=self.package,
            required_players=1,
            total_price_per_hour=30,
            total_amount=30,
            status=Order.STATUS_PENDING_PAYMENT,
        )
        self.wallet = ClientWallet.objects.create(
            profile=self.profile,
            balance=Decimal('30.00'),
            recharged_total=Decimal('30.00'),
        )
        self.recharge = RechargeOrder.objects.create(
            recharge_no='RCGCOINBACKED001',
            profile=self.profile,
            product=None,
            amount=Decimal('30.00'),
            channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
            status=RechargeOrder.STATUS_CREDITED,
            notify_payload={
                'mode': 'short_series_coin',
                'client_platform': 'ios',
                'wechat_coin_units_per_yuan': 100,
            },
        )
        ClientWalletLedger.objects.create(
            wallet=self.wallet,
            entry_type=ClientWalletLedger.TYPE_RECHARGE,
            amount=Decimal('30.00'),
            balance_after=Decimal('30.00'),
            reference_type='recharge_order',
            reference_id=self.recharge.recharge_no,
        )

    def test_coin_backed_balance_queries_then_deducts_remote_coin(self):
        def xpay(endpoint, payload, session_key):
            self.assertEqual(session_key, 'session-key')
            if endpoint == '/xpay/query_user_balance':
                return {'errcode': 0, 'balance': 3000}
            if endpoint == '/xpay/currency_pay':
                self.assertEqual(payload['amount'], 3000)
                self.assertEqual(payload['openid'], self.profile.openid)
                return {'errcode': 0, 'balance': 0}
            self.fail(f'unexpected XPay endpoint {endpoint}')

        with patch(
            'apps.wallet.coin_sync.exchange_code_for_session',
            return_value=(self.profile.openid, 'session-key'),
        ), patch('apps.wallet.coin_sync.user_xpay_post', side_effect=xpay) as mocked_xpay:
            result = pay_order_with_coin_aware_balance(
                self.order.order_no,
                self.user,
                code='fresh-code',
                user_ip='1.2.3.4',
            )

        self.assertEqual(result['status'], 'paid')
        self.assertEqual(result['wechat_coin_diamonds'], '300.0')
        self.assertEqual(result['wechat_coin_units'], 3000)
        self.assertEqual([call.args[0] for call in mocked_xpay.call_args_list], [
            '/xpay/query_user_balance',
            '/xpay/currency_pay',
        ])
        payment = Payment.objects.get(payment_no=result['payment_no'])
        self.assertEqual(payment.notify_payload['wechat_coin_status'], 'succeeded')
        self.assertEqual(payment.notify_payload['wechat_coin_diamonds'], '300.0')
        self.assertEqual(payment.notify_payload['wechat_coin_units'], 3000)
        self.assertEqual(payment.notify_payload['wechat_coin_balance_before'], 3000)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('0.00'))

    def test_successful_refund_is_cancelled_remotely_before_next_coin_spend(self):
        with patch(
            'apps.wallet.coin_sync.exchange_code_for_session',
            return_value=(self.profile.openid, 'session-key-1'),
        ), patch(
            'apps.wallet.coin_sync.user_xpay_post',
            side_effect=[
                {'errcode': 0, 'balance': 3000},
                {'errcode': 0, 'balance': 0},
            ],
        ):
            first = pay_order_with_coin_aware_balance(
                self.order.order_no,
                self.user,
                code='code-1',
            )
        payment = Payment.objects.get(payment_no=first['payment_no'])

        refund = Refund.objects.create(
            refund_no='REFCOIN001',
            payment=payment,
            order=self.order,
            amount=Decimal('15.00'),
            reason='部分退款',
            status=Refund.STATUS_SUCCEEDED,
            created_by=self.user,
        )
        refund.refresh_from_db()
        self.assertEqual(refund.notify_payload['wechat_coin_refund_units'], 1500)
        self.assertEqual(refund.notify_payload['wechat_coin_refund_diamonds'], '150.0')
        self.assertEqual(refund.notify_payload['wechat_coin_refund_status'], 'pending')
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('15.00'))

        second_order = Order.objects.create(
            order_no='COIN_BALANCE_ORDER_2',
            boss_user=self.user,
            boss_wechat='coin-boss',
            package=self.package,
            required_players=1,
            total_price_per_hour=10,
            total_amount=10,
            status=Order.STATUS_PENDING_PAYMENT,
        )

        def xpay(endpoint, payload, _session_key):
            if endpoint == '/xpay/cancel_currency_pay':
                # First partial local refund restores the complete original
                # currency_pay remotely because WeChat only permits one cancel.
                self.assertEqual(payload['amount'], 3000)
                self.assertEqual(payload['pay_order_id'], payment.notify_payload['wechat_coin_order_id'])
                return {'errcode': 0, 'balance': 3000}
            if endpoint == '/xpay/query_user_balance':
                return {'errcode': 0, 'balance': 3000}
            if endpoint == '/xpay/currency_pay':
                self.assertEqual(payload['amount'], 1000)
                return {'errcode': 0, 'balance': 2000}
            self.fail(f'unexpected XPay endpoint {endpoint}')

        with patch(
            'apps.wallet.coin_sync.exchange_code_for_session',
            return_value=(self.profile.openid, 'session-key-2'),
        ), patch('apps.wallet.coin_sync.user_xpay_post', side_effect=xpay) as mocked_xpay:
            second = pay_order_with_coin_aware_balance(
                second_order.order_no,
                self.user,
                code='code-2',
            )

        self.assertEqual(second['wechat_coin_diamonds'], '100.0')
        self.assertEqual(second['wechat_coin_units'], 1000)
        self.assertEqual([call.args[0] for call in mocked_xpay.call_args_list], [
            '/xpay/cancel_currency_pay',
            '/xpay/query_user_balance',
            '/xpay/currency_pay',
        ])
        refund.refresh_from_db()
        self.assertEqual(refund.notify_payload['wechat_coin_refund_status'], 'succeeded')
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('5.00'))


@override_settings(**VIRTUAL_SETTINGS)
class UnifiedCoinCheckoutApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='coin-checkout-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='openid_coin_checkout',
            nickname='统一Coin结算测试',
        )
        self.package = Package.objects.create(name='无需微信道具绑定套餐', player_count=1, base_price=12.3)
        self.order = Order.objects.create(
            order_no='COIN_CHECKOUT_ORDER_1',
            boss_user=self.user,
            boss_wechat='checkout-boss',
            package=self.package,
            required_players=1,
            total_price_per_hour=Decimal('12.30'),
            total_amount=Decimal('12.30'),
            status=Order.STATUS_PENDING_PAYMENT,
        )
        self.wallet = ClientWallet.objects.create(profile=self.profile)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_instant_checkout_recharges_coin_then_finalizes_without_product_binding(self):
        with patch(
            'apps.wallet.coin_recharge.exchange_code_for_session',
            return_value=(self.profile.openid, 'recharge-session'),
        ):
            created = self.client.post(
                '/api/pay/wechat/virtual/create',
                {'order_no': self.order.order_no, 'code': 'create-code'},
                format='json',
                HTTP_X_CLIENT_PLATFORM='ios',
            )
        self.assertEqual(created.status_code, 200, created.data)
        self.assertEqual(created.data['mode'], 'short_series_coin')
        self.assertEqual(created.data['diamonds'], '123.0')
        self.assertEqual(created.data['wechat_coin_units'], 1230)
        self.assertEqual(created.data['checkout_order_no'], self.order.order_no)
        self.assertNotIn('product_id', created.data)
        self.assertNotIn('productId', created.data['signData'])
        recharge_no = created.data['recharge_no']

        paid_query = {
            'errcode': 0,
            'errmsg': 'OK',
            'order': {
                'status': 3,
                'order_fee': 1230,
                'paid_fee': 1230,
                'wx_order_id': 'WX_COIN_CHECKOUT_001',
            },
        }
        with patch('apps.wallet.coin_recharge.xpay_post', return_value=paid_query):
            queried = self.client.post(
                f'/api/pay/wechat/virtual/query/{recharge_no}',
                {},
                format='json',
                HTTP_X_CLIENT_PLATFORM='ios',
            )
        self.assertEqual(queried.status_code, 200, queried.data)
        self.assertEqual(queried.data['status'], RechargeOrder.STATUS_CREDITED)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('12.30'))

        with patch(
            'apps.wallet.coin_sync.exchange_code_for_session',
            return_value=(self.profile.openid, 'spend-session'),
        ), patch(
            'apps.wallet.coin_sync.user_xpay_post',
            side_effect=[
                {'errcode': 0, 'balance': 1230},
                {'errcode': 0, 'balance': 0},
            ],
        ) as mocked_xpay:
            finalized = self.client.post(
                f'/api/pay/wechat/virtual/finalize/{recharge_no}',
                {'code': 'finalize-code'},
                format='json',
                HTTP_X_CLIENT_PLATFORM='ios',
            )

        self.assertEqual(finalized.status_code, 200, finalized.data)
        self.assertEqual(finalized.data['status'], 'paid')
        self.assertEqual(finalized.data['wechat_coin_diamonds'], '123.0')
        self.assertEqual(finalized.data['wechat_coin_units'], 1230)
        self.assertEqual([call.args[0] for call in mocked_xpay.call_args_list], [
            '/xpay/query_user_balance',
            '/xpay/currency_pay',
        ])
        self.order.refresh_from_db()
        self.assertTrue(self.order.paid)
        self.assertEqual(self.order.payment_method, 'balance')
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('0.00'))
