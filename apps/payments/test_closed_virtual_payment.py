from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order

from .models import Payment
from .views import reconcile_closed_virtual_payment
from .virtualpay import VirtualPaymentAPIError


VIRTUAL_SETTINGS = {
    'WECHAT_VIRTUALPAY_ENABLED': True,
    'WECHAT_APP_ID': 'wx_test_appid',
    'WECHAT_APP_SECRET': 'test_secret',
    'WECHAT_VIRTUALPAY_OFFER_ID': 'test_offer',
    'WECHAT_VIRTUALPAY_APP_KEY': 'test_app_key',
    'WECHAT_VIRTUALPAY_ENV': 0,
    'WECHAT_VIRTUALPAY_HTTP_TIMEOUT': 10,
}


@override_settings(**VIRTUAL_SETTINGS)
class ClosedVirtualPaymentRecoveryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='closed-virtual-user')
        ClientProfile.objects.create(
            user=self.user,
            openid='openid_closed_virtual',
            nickname='关闭支付单测试用户',
        )
        package = Package.objects.create(
            name='关闭支付单测试商品',
            player_count=1,
            base_price=30,
            is_active=True,
        )
        self.order = Order.objects.create(
            order_no='CLOSED_VIRTUAL_ORDER_001',
            boss_user=self.user,
            boss_wechat='closed_virtual_user',
            package=package,
            required_players=1,
            total_price_per_hour=30,
            total_amount=30,
            status=Order.STATUS_PENDING_PAYMENT,
        )
        self.payment = Payment.objects.create(
            payment_no='CLOSED_VIRTUAL_PAY_001',
            order=self.order,
            channel='wechat_virtual',
            scene='short_series_goods',
            amount=30,
            status='paying',
            third_order_no='CLOSED_VIRTUAL_PAY_001',
            notify_payload={'virtual_payment': True},
        )

    @patch('apps.payments.views.query_virtual_payment')
    def test_remote_closed_order_is_not_reused(self, mocked_query):
        self.payment.notify_payload = {
            'virtual_payment': True,
            'query_order': {'status': 6},
        }
        mocked_query.return_value = self.payment

        changed = reconcile_closed_virtual_payment(self.order.order_no, self.user)

        self.payment.refresh_from_db()
        self.assertTrue(changed)
        self.assertEqual(self.payment.status, 'closed')

    @patch('apps.payments.views.query_virtual_payment')
    def test_pending_remote_order_remains_reusable(self, mocked_query):
        self.payment.notify_payload = {
            'virtual_payment': True,
            'query_order': {'status': 1},
        }
        mocked_query.return_value = self.payment

        changed = reconcile_closed_virtual_payment(self.order.order_no, self.user)

        self.payment.refresh_from_db()
        self.assertFalse(changed)
        self.assertEqual(self.payment.status, 'paying')

    @patch('apps.payments.views.query_virtual_payment')
    def test_order_closed_api_error_is_not_reused(self, mocked_query):
        mocked_query.side_effect = VirtualPaymentAPIError(-15012, 'ORDER_CLOSED')

        changed = reconcile_closed_virtual_payment(self.order.order_no, self.user)

        self.payment.refresh_from_db()
        self.assertTrue(changed)
        self.assertEqual(self.payment.status, 'closed')
