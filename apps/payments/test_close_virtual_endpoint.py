from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order

from .models import Payment


class CloseVirtualPaymentEndpointTests(TestCase):
    """B6：用户主动关闭进行中的微信虚拟支付单（取消收银台后改用余额支付）。"""

    def setUp(self):
        self.user = User.objects.create_user(username='close-boss')
        ClientProfile.objects.create(
            user=self.user,
            openid='openid_close_boss',
            nickname='关单测试老板',
        )
        package = Package.objects.create(name='关单测试套餐', player_count=1, base_price=30)
        self.order = Order.objects.create(
            order_no='CLOSE_ORDER_001',
            boss_user=self.user,
            boss_wechat='close_boss',
            package=package,
            required_players=1,
            total_price_per_hour=30,
            total_amount=30,
            status=Order.STATUS_PENDING_PAYMENT,
        )
        self.payment = Payment.objects.create(
            payment_no='VIRTUAL_CLOSE_001',
            order=self.order,
            channel='wechat_virtual',
            scene='short_series_goods',
            amount=30,
            status='paying',
            expires_at=timezone.now() + timedelta(minutes=9),
        )
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def _close(self, api=None):
        return (api or self.api).post(
            f'/api/pay/wechat/virtual/close/{self.payment.payment_no}'
        )

    def test_owner_closes_paying_virtual_payment(self):
        response = self._close()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'closed')
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, 'closed')

    def test_close_is_idempotent(self):
        self._close()
        response = self._close()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'closed')

    def test_non_owner_rejected(self):
        other = User.objects.create_user(username='close-other')
        api = APIClient()
        api.force_authenticate(other)
        response = self._close(api)
        self.assertIn(response.status_code, (400, 403, 404))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, 'paying')

    def test_paid_payment_not_closed(self):
        self.payment.status = 'paid'
        self.payment.save(update_fields=['status', 'updated_at'])
        response = self._close()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'paid')
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, 'paid')

    def test_non_virtual_channel_rejected(self):
        self.payment.channel = 'wechat'
        self.payment.scene = 'jsapi'
        self.payment.save(update_fields=['channel', 'scene', 'updated_at'])
        response = self._close()
        self.assertEqual(response.status_code, 400)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, 'paying')
