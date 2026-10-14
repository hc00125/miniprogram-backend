from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.payments.models import Payment

from .models import Order


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
class PendingOrderPaymentReconciliationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='reconcile-boss', password='pass')
        ClientProfile.objects.create(
            user=self.user,
            openid='openid_reconcile_boss',
            nickname='支付核验测试老板',
        )
        package = Package.objects.create(name='支付核验测试套餐', player_count=1, base_price=35)
        self.order = Order.objects.create(
            order_no='RECONCILE_ORDER_001',
            boss_user=self.user,
            boss_wechat='reconcile_boss',
            package=package,
            required_players=1,
            total_price_per_hour=35,
            total_amount=35,
            status=Order.STATUS_PENDING_PAYMENT,
        )
        self.payment = Payment.objects.create(
            payment_no='RECONCILE_PAY_001',
            order=self.order,
            channel='wechat_virtual',
            scene='short_series_goods',
            amount=35,
            status='paying',
            third_order_no='RECONCILE_PAY_001',
            notify_payload={
                'virtual_payment': True,
                'env': 0,
                'delivery_status': 'pending',
                'product_id': 'nvpei_44',
                'goods_price_fen': 3500,
                'quantity': 1,
                'expected_total_fen': 3500,
            },
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch('apps.payments.virtualpay.xpay_post')
    def test_order_detail_recovers_paid_virtual_payment(self, mocked_xpay_post):
        mocked_xpay_post.side_effect = [
            {
                'errcode': 0,
                'errmsg': 'OK',
                'order': {
                    'status': 3,
                    'order_fee': 3500,
                    'paid_fee': 3500,
                    'wx_order_id': 'WX_RECONCILE_001',
                },
            },
            {'errcode': 0, 'errmsg': 'OK'},
        ]

        response = self.client.get('/api/boss/order/RECONCILE_ORDER_001')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['paid'])
        self.assertEqual(response.data['status'], Order.STATUS_READY_TO_START)

        self.payment.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.payment.status, 'paid')
        self.assertTrue(self.order.paid)
        self.assertEqual(self.order.status, Order.STATUS_READY_TO_START)

    @patch('apps.payments.virtualpay.xpay_post')
    def test_order_detail_still_returns_when_wechat_query_temporarily_fails(self, mocked_xpay_post):
        mocked_xpay_post.side_effect = Exception('temporary network failure')

        response = self.client.get('/api/boss/order/RECONCILE_ORDER_001')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['paid'])
        self.assertEqual(response.data['status'], Order.STATUS_PENDING_PAYMENT)
