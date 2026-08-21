import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.exceptions import ValidationError

from apps.accounts.models import ClientProfile

from .coin_recharge import VIRTUAL_MODE_COIN, create_coin_recharge, query_coin_recharge
from .models import ClientWallet, ClientWalletLedger, RechargeOrder


VIRTUAL_SETTINGS = {
    'WECHAT_VIRTUALPAY_ENABLED': True,
    'WECHAT_APP_ID': 'wx_test_appid',
    'WECHAT_APP_SECRET': 'test_secret',
    'WECHAT_VIRTUALPAY_OFFER_ID': 'test_offer',
    'WECHAT_VIRTUALPAY_APP_KEY': 'test_app_key',
    'WECHAT_VIRTUALPAY_ENV': 0,
    'WECHAT_VIRTUALPAY_HTTP_TIMEOUT': 10,
    'ENABLE_MOCK_PAYMENT': False,
}


@override_settings(**VIRTUAL_SETTINGS)
class CoinRechargeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='coin-recharge-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='openid_coin_recharge',
            nickname='自由充值测试老板',
        )

    def _create(self, amount='37.20', platform='android'):
        with patch(
            'apps.wallet.coin_recharge.exchange_code_for_session',
            return_value=(self.profile.openid, 'session-key'),
        ):
            return create_coin_recharge(self.user, amount, 'wx-code', platform)

    def test_arbitrary_amount_builds_coin_payment_without_fixed_product(self):
        recharge, payload = self._create('37.20')

        self.assertIsNone(recharge.product_id)
        self.assertEqual(recharge.amount, Decimal('37.20'))
        self.assertEqual(payload['mode'], VIRTUAL_MODE_COIN)
        self.assertEqual(payload['diamonds'], 372)
        sign_data = json.loads(payload['signData'])
        self.assertEqual(sign_data['buyQuantity'], 372)
        self.assertNotIn('productId', sign_data)
        self.assertNotIn('goodsPrice', sign_data)

    def test_amount_must_map_to_integer_diamonds(self):
        with self.assertRaises(ValidationError):
            self._create('37.25')

    def test_ios_minimum_is_one_yuan(self):
        with self.assertRaises(ValidationError):
            self._create('0.90', platform='ios')

    @override_settings(WECHAT_VIRTUALPAY_ENV=1)
    def test_ios_rejects_sandbox(self):
        with self.assertRaises(ValidationError):
            self._create('10.00', platform='ios')

    def test_paid_query_credits_once_and_does_not_send_goods_delivery(self):
        recharge, _payload = self._create('18.80')
        paid = {
            'errcode': 0,
            'errmsg': 'OK',
            'order': {
                'status': 3,
                'order_fee': 1880,
                'paid_fee': 1880,
                'wx_order_id': 'WX_COIN_001',
            },
        }
        with patch('apps.wallet.coin_recharge.xpay_post', return_value=paid) as xpay:
            synced = query_coin_recharge(recharge.recharge_no, self.user)

        self.assertEqual(synced.status, RechargeOrder.STATUS_CREDITED)
        self.assertEqual(xpay.call_count, 1)
        wallet = ClientWallet.objects.get(profile=self.profile)
        self.assertEqual(wallet.balance, Decimal('18.80'))
        self.assertEqual(wallet.recharged_total, Decimal('18.80'))
        self.assertEqual(ClientWalletLedger.objects.filter(
            entry_type=ClientWalletLedger.TYPE_RECHARGE,
            reference_id=recharge.recharge_no,
        ).count(), 1)

        with patch('apps.wallet.coin_recharge.xpay_post') as replay_xpay:
            replayed = query_coin_recharge(recharge.recharge_no, self.user)
        self.assertEqual(replayed.status, RechargeOrder.STATUS_CREDITED)
        replay_xpay.assert_not_called()
        self.assertEqual(ClientWalletLedger.objects.filter(
            entry_type=ClientWalletLedger.TYPE_RECHARGE,
            reference_id=recharge.recharge_no,
        ).count(), 1)
