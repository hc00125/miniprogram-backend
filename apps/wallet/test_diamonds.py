from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import ClientProfile

from .diamonds import (
    coin_units_to_yuan,
    diamonds_to_yuan,
    format_diamonds,
    yuan_to_coin_units,
    yuan_to_diamonds,
)
from .models import ClientWallet, ClientWalletLedger, RechargeOrder, RechargeProduct


class DiamondConversionTests(TestCase):
    def test_yuan_to_one_decimal_diamonds_without_float_math(self):
        self.assertEqual(yuan_to_diamonds(Decimal('10.00')), Decimal('100.0'))
        self.assertEqual(yuan_to_diamonds(Decimal('19.90')), Decimal('199.0'))
        self.assertEqual(yuan_to_diamonds(Decimal('19.99')), Decimal('199.9'))
        self.assertEqual(format_diamonds(Decimal('19.99')), '199.9')
        self.assertEqual(diamonds_to_yuan(Decimal('199.9')), Decimal('19.99'))

    @override_settings(WECHAT_VIRTUALPAY_COIN_UNITS_PER_YUAN=100)
    def test_cent_amount_maps_exactly_to_integer_xpay_units(self):
        self.assertEqual(yuan_to_coin_units(Decimal('19.99')), 1999)
        self.assertEqual(
            coin_units_to_yuan(1999, units_per_yuan=100),
            Decimal('19.99'),
        )

    def test_legacy_xpay_scale_rejects_unrepresentable_cent_amount(self):
        with self.assertRaises(DjangoValidationError):
            yuan_to_coin_units(Decimal('19.99'), units_per_yuan=10)

    def test_recharge_product_only_accepts_fixed_tiers(self):
        valid = RechargeProduct(
            amount=Decimal('10.00'),
            product_id='test_fixed_recharge_10',
            goods_price_fen=1000,
        )
        valid.full_clean()
        self.assertEqual(valid.diamond_amount, Decimal('100.0'))

        invalid = RechargeProduct(
            amount=Decimal('20.00'),
            product_id='test_invalid_recharge_20',
            goods_price_fen=2000,
        )
        with self.assertRaises(DjangoValidationError):
            invalid.full_clean()


class DiamondWalletApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='diamond-wallet-user')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='diamond-wallet-openid',
            nickname='钻石钱包测试',
        )
        self.wallet = ClientWallet.objects.create(
            profile=self.profile,
            balance=Decimal('70.01'),
            recharged_total=Decimal('100.01'),
            spent_total=Decimal('30.00'),
        )
        ClientWalletLedger.objects.create(
            wallet=self.wallet,
            entry_type=ClientWalletLedger.TYPE_ORDER_PAYMENT,
            amount=Decimal('-29.99'),
            balance_after=Decimal('70.01'),
            reference_type='payment',
            reference_id='PAY-DIAMOND-001',
        )
        self.client_api = APIClient()
        self.client_api.force_authenticate(self.user)

    def test_overview_returns_one_decimal_diamond_strings_and_exact_yuan(self):
        response = self.client_api.get('/api/client/wallet/overview')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['balance_diamonds'], '700.1')
        self.assertEqual(response.data['recharged_total_diamonds'], '1000.1')
        self.assertEqual(response.data['spent_total_diamonds'], '300.0')
        self.assertEqual(response.data['balance_yuan'], '70.01')
        self.assertEqual(response.data['diamonds_per_yuan'], 10)
        self.assertIsInstance(response.data['balance_diamonds'], str)

    def test_wallet_transactions_return_signed_one_decimal_diamonds(self):
        response = self.client_api.get('/api/client/wallet/transactions')
        self.assertEqual(response.status_code, 200)
        item = response.data['results'][0]
        self.assertEqual(item['amount_diamonds'], '-299.9')
        self.assertEqual(item['balance_after_diamonds'], '700.1')

    def test_recharge_packages_only_expose_active_fixed_tiers(self):
        RechargeProduct.objects.update_or_create(
            product_id='test_api_recharge_10',
            defaults={
                'amount': Decimal('10.00'),
                'goods_price_fen': 1000,
                'is_active': True,
                'sort_order': 1,
            },
        )
        # 直接写入模拟历史非标准档位；API仍不得向用户展示。
        RechargeProduct.objects.create(
            product_id='test_api_recharge_20',
            amount=Decimal('20.00'),
            goods_price_fen=2000,
            is_active=True,
            sort_order=2,
        )
        response = self.client_api.get('/api/client/wallet/recharge/packages')
        self.assertEqual(response.status_code, 200)
        amounts = {item['diamonds'] for item in response.data['results']}
        self.assertIn('100.0', amounts)
        self.assertNotIn('200.0', amounts)
        self.assertTrue(all(isinstance(item['diamonds'], str) for item in response.data['results']))

    def test_recharge_history_survives_client_storage_loss(self):
        RechargeOrder.objects.create(
            recharge_no='RCGDIAMONDHISTORY001',
            profile=self.profile,
            amount=Decimal('30.01'),
            status=RechargeOrder.STATUS_CREDITED,
        )
        response = self.client_api.get('/api/client/wallet/recharge/orders')
        self.assertEqual(response.status_code, 200)
        item = response.data['results'][0]
        self.assertEqual(item['diamonds'], '300.1')
        self.assertEqual(item['pay_amount_yuan'], '30.01')

    @override_settings(ENABLE_MOCK_PAYMENT=True, WECHAT_VIRTUALPAY_ENABLED=False)
    def test_new_recharge_product_id_request_field_is_supported(self):
        product = RechargeProduct.objects.create(
            product_id='test_new_request_field_30',
            amount=Decimal('30.00'),
            goods_price_fen=3000,
            is_active=True,
        )
        response = self.client_api.post(
            '/api/client/wallet/recharge/create',
            {'recharge_product_id': product.id, 'code': ''},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['diamonds'], '300.0')
        self.assertEqual(response.data['pay_amount_yuan'], '30.00')
