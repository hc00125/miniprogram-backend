from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import ClientProfile

from .diamonds import diamonds_to_yuan, yuan_to_diamonds
from .models import ClientWallet, ClientWalletLedger, RechargeOrder, RechargeProduct


class DiamondConversionTests(TestCase):
    def test_yuan_to_integer_diamonds_without_float_math(self):
        self.assertEqual(yuan_to_diamonds(Decimal('10.00')), 100)
        self.assertEqual(yuan_to_diamonds(Decimal('19.90')), 199)
        self.assertEqual(diamonds_to_yuan(199), Decimal('19.90'))

    def test_fractional_diamonds_are_rejected(self):
        with self.assertRaises(DjangoValidationError):
            yuan_to_diamonds(Decimal('19.99'))

    def test_recharge_product_only_accepts_fixed_tiers(self):
        valid = RechargeProduct(
            amount=Decimal('10.00'),
            product_id='test_fixed_recharge_10',
            goods_price_fen=1000,
        )
        valid.full_clean()
        self.assertEqual(valid.diamond_amount, 100)

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
            balance=Decimal('70.00'),
            recharged_total=Decimal('100.00'),
            spent_total=Decimal('30.00'),
        )
        ClientWalletLedger.objects.create(
            wallet=self.wallet,
            entry_type=ClientWalletLedger.TYPE_ORDER_PAYMENT,
            amount=Decimal('-30.00'),
            balance_after=Decimal('70.00'),
            reference_type='payment',
            reference_id='PAY-DIAMOND-001',
        )
        self.client_api = APIClient()
        self.client_api.force_authenticate(self.user)

    def test_overview_returns_integer_diamond_fields_and_legacy_yuan(self):
        response = self.client_api.get('/api/client/wallet/overview')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['balance_diamonds'], 700)
        self.assertEqual(response.data['recharged_total_diamonds'], 1000)
        self.assertEqual(response.data['spent_total_diamonds'], 300)
        self.assertEqual(response.data['balance_yuan'], '70.00')
        self.assertEqual(response.data['diamonds_per_yuan'], 10)
        self.assertIsInstance(response.data['balance_diamonds'], int)

    def test_wallet_transactions_return_signed_integer_diamonds(self):
        response = self.client_api.get('/api/client/wallet/transactions')
        self.assertEqual(response.status_code, 200)
        item = response.data['results'][0]
        self.assertEqual(item['amount_diamonds'], -300)
        self.assertEqual(item['balance_after_diamonds'], 700)

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
        self.assertIn(100, amounts)
        self.assertNotIn(200, amounts)
        self.assertTrue(all(isinstance(item['diamonds'], int) for item in response.data['results']))

    def test_recharge_history_survives_client_storage_loss(self):
        RechargeOrder.objects.create(
            recharge_no='RCGDIAMONDHISTORY001',
            profile=self.profile,
            amount=Decimal('30.00'),
            status=RechargeOrder.STATUS_CREDITED,
        )
        response = self.client_api.get('/api/client/wallet/recharge/orders')
        self.assertEqual(response.status_code, 200)
        item = response.data['results'][0]
        self.assertEqual(item['diamonds'], 300)
        self.assertEqual(item['pay_amount_yuan'], '30.00')

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
        self.assertEqual(response.data['diamonds'], 300)
        self.assertEqual(response.data['pay_amount_yuan'], '30.00')
