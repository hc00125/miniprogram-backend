from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.catalog.models import Package
from apps.orders.models import Order, OrderStatusLog
from apps.orders.payment_deadlines import persist_payment_window

from .models import Payment


class PaymentDeadlineAlignmentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='payment-expiry-alignment-user')
        self.package = Package.objects.create(
            name='支付失效时间对齐测试商品',
            player_count=1,
            base_price=15,
            is_active=True,
        )
        self.order = Order.objects.create(
            order_no='PAYMENT_ALIGNMENT_01',
            boss_user=self.user,
            boss_wechat='payment-alignment-openid',
            package=self.package,
            required_players=1,
            total_price_per_hour=15,
            total_amount=15,
            status=Order.STATUS_PENDING_PAYMENT,
            paid=False,
        )
        started_at = timezone.now() - timedelta(minutes=8)
        log = OrderStatusLog.objects.create(
            order=self.order,
            to_status=Order.STATUS_PENDING_PAYMENT,
            reason='测试进入待支付',
        )
        OrderStatusLog.objects.filter(pk=log.pk).update(created_at=started_at)
        self.window = persist_payment_window(
            self.order,
            started_at=started_at,
            force_reset=True,
        )

    def test_new_payment_is_clamped_to_business_deadline(self):
        payment = Payment.objects.create(
            payment_no='PAYMENT_ALIGNMENT_PAY_01',
            order=self.order,
            channel='wechat_virtual',
            scene='short_series_goods',
            amount=15,
            status='paying',
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        self.assertEqual(payment.expires_at, self.window.deadline_at)
        self.assertLess(payment.expires_at, timezone.now() + timedelta(minutes=3))

    def test_earlier_payment_expiry_is_not_extended(self):
        earlier_expiry = timezone.now() + timedelta(seconds=30)
        payment = Payment.objects.create(
            payment_no='PAYMENT_ALIGNMENT_PAY_02',
            order=self.order,
            channel='wechat_virtual',
            scene='short_series_goods',
            amount=15,
            status='paying',
            expires_at=earlier_expiry,
        )

        self.assertEqual(payment.expires_at, earlier_expiry)
