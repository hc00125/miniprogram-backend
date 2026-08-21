from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order

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
class CoinRemoteSuccessRetryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='coin-idempotency-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='openid_coin_idempotency',
            nickname='Coin幂等恢复老板',
        )
        self.package = Package.objects.create(name='Coin幂等恢复商品', player_count=1, base_price=20)
        self.order = Order.objects.create(
            order_no='COINIDEMPOTENT001',
            boss_user=self.user,
            boss_wechat='coin-idempotency',
            package=self.package,
            required_players=1,
            total_price_per_hour=20,
            total_amount=20,
            status=Order.STATUS_PENDING_PAYMENT,
        )
        self.wallet = ClientWallet.objects.create(
            profile=self.profile,
            balance=Decimal('20.00'),
            recharged_total=Decimal('20.00'),
        )
        recharge = RechargeOrder.objects.create(
            recharge_no='RCGCOINIDEMPOTENT1',
            profile=self.profile,
            product=None,
            amount=Decimal('20.00'),
            channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
            status=RechargeOrder.STATUS_CREDITED,
            notify_payload={
                'mode': 'short_series_coin',
                'wechat_coin_units_per_yuan': 100,
            },
        )
        ClientWalletLedger.objects.create(
            wallet=self.wallet,
            entry_type=ClientWalletLedger.TYPE_RECHARGE,
            amount=Decimal('20.00'),
            balance_after=Decimal('20.00'),
            reference_type='recharge_order',
            reference_id=recharge.recharge_no,
        )

    def test_low_remote_balance_still_replays_deterministic_currency_pay(self):
        """模拟上一次微信已扣币、本地事务却未落账后的重试。"""
        def xpay(endpoint, payload, _session_key):
            if endpoint == '/xpay/query_user_balance':
                # 微信侧上次已经扣了2000个最小单位，所以现在余额为0。
                return {'errcode': 0, 'balance': 0}
            if endpoint == '/xpay/currency_pay':
                self.assertEqual(payload['amount'], 2000)
                return {'errcode': 268490004, 'errmsg': 'duplicate success'}
            self.fail(f'unexpected endpoint {endpoint}')

        with patch(
            'apps.wallet.coin_sync.exchange_code_for_session',
            return_value=(self.profile.openid, 'retry-session'),
        ), patch('apps.wallet.coin_sync.user_xpay_post', side_effect=xpay) as mocked_xpay:
            result = pay_order_with_coin_aware_balance(
                self.order.order_no,
                self.user,
                code='fresh-retry-code',
            )

        self.assertEqual(result['status'], 'paid')
        self.assertEqual(result['wechat_coin_diamonds'], '200.0')
        self.assertEqual(result['wechat_coin_units'], 2000)
        self.assertEqual(result['wechat_coin_units_per_yuan'], 100)
        self.assertEqual(
            [call.args[0] for call in mocked_xpay.call_args_list],
            ['/xpay/query_user_balance', '/xpay/currency_pay'],
        )
        payment = self.order.payments.get(status='paid')
        self.assertTrue(payment.notify_payload['wechat_coin_balance_mismatch'])
        self.assertEqual(payment.notify_payload['wechat_coin_order_id'][:2], 'CP')
        self.assertEqual(payment.notify_payload['wechat_coin_units'], 2000)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('0.00'))
