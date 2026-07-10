from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order, OrderItem

from .virtualpay import hmac_sha256_hex, resolve_virtual_product


@override_settings(
    WECHAT_VIRTUALPAY_ENV=1,
    WECHAT_VIRTUALPAY_SANDBOX_PRODUCT_ID='escort_15',
    WECHAT_VIRTUALPAY_SANDBOX_PRICE_FEN=1500,
    WECHAT_VIRTUALPAY_SANDBOX_PACKAGE_KEYWORD='四套四弹',
)
class VirtualPaymentSandboxTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='virtual-pay-user')
        ClientProfile.objects.create(user=self.user, openid='openid-test', nickname='测试老板')
        self.package = Package.objects.create(
            name='四套四弹娱乐陪',
            base_price=15,
            player_count=4,
            is_active=True,
        )
        self.order = Order.objects.create(
            order_no='20260710123456ABCD',
            boss_user=self.user,
            boss_wechat='test-wechat',
            package=self.package,
            required_players=4,
            total_price_per_hour=15,
            total_amount=15,
            status=Order.STATUS_PENDING_PAYMENT,
        )
        OrderItem.objects.create(
            order=self.order,
            package=self.package,
            package_name=self.package.name,
            unit_price=15,
            quantity=1,
            amount=15,
        )

    def test_sandbox_fallback_resolves_escort_15(self):
        result = resolve_virtual_product(self.order)
        self.assertEqual(result['product_id'], 'escort_15')
        self.assertEqual(result['goods_price_fen'], 1500)
        self.assertEqual(result['quantity'], 1)
        self.assertEqual(result['expected_total_fen'], 1500)

    def test_hmac_signature_is_stable(self):
        result = hmac_sha256_hex('key', 'requestVirtualPayment&{}')
        self.assertEqual(len(result), 64)
        self.assertEqual(result, hmac_sha256_hex('key', 'requestVirtualPayment&{}'))
