import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.accounts.models import ClientProfile

from .coin_recharge import VIRTUAL_MODE_COIN, create_coin_recharge, query_coin_recharge
from .models import ClientWallet, ClientWalletLedger, RechargeOrder, RechargeProduct


VIRTUAL_SETTINGS = {
    'WECHAT_VIRTUALPAY_ENABLED': True,
    'WECHAT_APP_ID': 'wx_test_appid',
    'WECHAT_APP_SECRET': 'test_secret',
    'WECHAT_VIRTUALPAY_OFFER_ID': 'test_offer',
    'WECHAT_VIRTUALPAY_APP_KEY': 'test_app_key',
    'WECHAT_VIRTUALPAY_ENV': 0,
    'WECHAT_VIRTUALPAY_HTTP_TIMEOUT': 10,
    'WECHAT_VIRTUALPAY_COIN_UNITS_PER_YUAN': 10,
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
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _create(self, amount='37.20', platform='android'):
        with patch(
            'apps.wallet.coin_recharge.exchange_code_for_session',
            return_value=(self.profile.openid, 'session-key'),
        ):
            return create_coin_recharge(self.user, amount, 'wx-code', platform)

    def test_five_yuan_builds_fifty_published_wechat_coins(self):
        recharge, payload = self._create('5.00')

        self.assertIsNone(recharge.product_id)
        self.assertEqual(recharge.amount, Decimal('5.00'))
        self.assertEqual(payload['mode'], VIRTUAL_MODE_COIN)
        self.assertEqual(payload['diamonds'], '50.0')
        self.assertEqual(payload['wechat_coin_units'], 50)
        self.assertEqual(payload['wechat_coin_units_per_yuan'], 10)
        sign_data = json.loads(payload['signData'])
        self.assertEqual(sign_data['buyQuantity'], 50)
        self.assertNotIn('productId', sign_data)
        self.assertNotIn('goodsPrice', sign_data)

    def test_order_checkout_relation_is_persisted_in_indexed_field(self):
        with patch(
            'apps.wallet.coin_recharge.exchange_code_for_session',
            return_value=(self.profile.openid, 'session-key'),
        ):
            recharge, payload = create_coin_recharge(
                self.user,
                '6.00',
                'wx-code',
                'android',
                checkout_order_no='ORDER-CHECKOUT-001',
            )

        self.assertEqual(recharge.checkout_order_no, 'ORDER-CHECKOUT-001')
        self.assertEqual(payload['checkout_order_no'], 'ORDER-CHECKOUT-001')

    def test_credited_checkout_cannot_create_second_recharge(self):
        RechargeOrder.objects.create(
            recharge_no='RCG-PAID-CHECKOUT-001',
            profile=self.profile,
            product=None,
            amount=Decimal('6.00'),
            channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
            status=RechargeOrder.STATUS_CREDITED,
            checkout_order_no='ORDER-CHECKOUT-PAID',
            notify_payload={
                'mode': VIRTUAL_MODE_COIN,
                'checkout_order_no': 'ORDER-CHECKOUT-PAID',
                'wechat_coin_units_per_yuan': 10,
            },
        )

        with self.assertRaises(ValidationError) as context:
            create_coin_recharge(
                self.user,
                '6.00',
                'new-code',
                'android',
                checkout_order_no='ORDER-CHECKOUT-PAID',
            )

        self.assertIn('请勿重复支付', str(context.exception.detail))
        self.assertEqual(
            RechargeOrder.objects.filter(checkout_order_no='ORDER-CHECKOUT-PAID').count(),
            1,
        )

    def test_unrepresentable_cent_amount_is_rejected_without_rounding(self):
        with patch(
            'apps.wallet.coin_recharge.exchange_code_for_session',
            return_value=(self.profile.openid, 'session-key'),
        ):
            response = self.client.post(
                '/api/client/wallet/recharge/create',
                {'amount_yuan': '37.25', 'code': 'wx-code'},
                format='json',
                HTTP_X_CLIENT_PLATFORM='android',
            )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('0.10', str(response.data))
        self.assertFalse(RechargeOrder.objects.filter(amount=Decimal('37.25')).exists())

    def test_legacy_product_id_is_converted_to_unified_coin_recharge(self):
        product = RechargeProduct.objects.create(
            amount=Decimal('30.00'),
            product_id='legacy_recharge_30',
            goods_price_fen=3000,
            is_active=True,
        )
        with patch(
            'apps.wallet.coin_recharge.exchange_code_for_session',
            return_value=(self.profile.openid, 'session-key'),
        ):
            response = self.client.post(
                '/api/client/wallet/recharge/create',
                {'recharge_product_id': product.id, 'code': 'wx-code'},
                format='json',
                HTTP_X_CLIENT_PLATFORM='android',
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['pay_amount_yuan'], '30.00')
        self.assertEqual(response.data['diamonds'], '300.0')
        self.assertEqual(response.data['wechat_coin_units'], 300)
        self.assertEqual(response.data['wechat_coin_units_per_yuan'], 10)
        self.assertEqual(response.data['mode'], VIRTUAL_MODE_COIN)
        recharge = RechargeOrder.objects.get(recharge_no=response.data['recharge_no'])
        self.assertIsNone(recharge.product_id)
        self.assertEqual(recharge.amount, Decimal('30.00'))

    def test_recharge_config_exposes_published_scale_and_legacy_product_id(self):
        product = RechargeProduct.objects.create(
            amount=Decimal('30.00'),
            product_id='legacy_config_30',
            goods_price_fen=3000,
            is_active=True,
        )
        response = self.client.get(
            '/api/client/wallet/recharge/packages',
            HTTP_X_CLIENT_PLATFORM='android',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['amount_step_yuan'], '0.10')
        self.assertEqual(response.data['diamond_step'], '1.0')
        self.assertEqual(response.data['wechat_coin_units_per_yuan'], 10)
        item = next(item for item in response.data['results'] if item['pay_amount_yuan'] == '30.00')
        self.assertEqual(item['id'], product.id)
        self.assertEqual(item['legacy_recharge_product_id'], product.id)

    def test_ios_minimum_is_one_yuan_for_core_xpay_path(self):
        with self.assertRaises(ValidationError):
            self._create('0.90', platform='ios')

    def test_ios_config_only_exposes_10_and_30_at_seven_diamonds_per_yuan(self):
        response = self.client.get(
            '/api/client/wallet/recharge/packages',
            HTTP_X_CLIENT_PLATFORM='ios',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['effective_diamonds_per_yuan'], '7.0')
        self.assertFalse(response.data['custom_amount_enabled'])
        self.assertEqual(response.data['allowed_amounts_yuan'], ['10.00', '30.00'])
        self.assertEqual(
            [(item['pay_amount_yuan'], item['diamonds']) for item in response.data['results']],
            [('10.00', '70.0'), ('30.00', '210.0')],
        )

    def test_ios_standalone_recharge_rejects_custom_amount(self):
        response = self.client.post(
            '/api/client/wallet/recharge/create',
            {'amount_yuan': '20.00', 'code': 'wx-code'},
            format='json',
            HTTP_X_CLIENT_PLATFORM='ios',
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('仅支持10元或30元', str(response.data))

    def test_ios_30_yuan_recharge_returns_210_diamonds(self):
        with patch(
            'apps.wallet.coin_recharge.exchange_code_for_session',
            return_value=(self.profile.openid, 'session-key'),
        ):
            response = self.client.post(
                '/api/client/wallet/recharge/create',
                {'amount_yuan': '30.00', 'code': 'wx-code'},
                format='json',
                HTTP_X_CLIENT_PLATFORM='ios',
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['pay_amount_yuan'], '30.00')
        self.assertEqual(response.data['diamonds'], '210.0')
        recharge = RechargeOrder.objects.get(recharge_no=response.data['recharge_no'])
        self.assertEqual(recharge.notify_payload['ios_credit_diamonds_per_yuan'], '7.00')
        self.assertTrue(recharge.notify_payload['ios_fixed_credit'])

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