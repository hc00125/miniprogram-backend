from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order

from .models import Payment, Refund


class PaymentAdminSearchTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='payment-admin',
            email='admin@example.com',
            password='test-pass',
        )
        self.user = User.objects.create_user(username='chen-user')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='openid-chen-search',
            nickname='chen老板',
        )
        self.package = Package.objects.create(
            name='后台搜索测试套餐',
            player_count=1,
            base_price=10,
        )
        self.order = Order.objects.create(
            order_no='ADMIN_SEARCH_ORDER_001',
            boss_user=self.user,
            boss_wechat='chen-search',
            package=self.package,
            required_players=1,
            total_price_per_hour=Decimal('10.00'),
            total_amount=Decimal('10.00'),
            status=Order.STATUS_READY_TO_START,
            paid=True,
            payment_method='wechat_virtual',
        )
        self.payment = Payment.objects.create(
            payment_no='ADMIN_SEARCH_PAY_001',
            order=self.order,
            channel='wechat_virtual',
            scene='short_series_goods',
            amount=Decimal('10.00'),
            status='paid',
            third_trade_no='WX_ADMIN_SEARCH_001',
        )
        self.refund = Refund.objects.create(
            refund_no='ADMIN_SEARCH_REF_001',
            payment=self.payment,
            order=self.order,
            amount=Decimal('10.00'),
            status=Refund.STATUS_SUCCEEDED,
            reason='后台搜索测试退款',
        )
        self.client.force_login(self.admin)

    def test_payment_admin_text_search_does_not_treat_text_as_integer_id(self):
        response = self.client.get('/admin/payments/payment/', {'q': 'chen'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.payment.payment_no)

    def test_payment_admin_numeric_search_matches_django_user_id(self):
        response = self.client.get('/admin/payments/payment/', {'q': str(self.user.id)})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.payment.payment_no)

    def test_payment_admin_numeric_search_matches_client_profile_id(self):
        response = self.client.get('/admin/payments/payment/', {'q': str(self.profile.id)})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.payment.payment_no)

    def test_refund_admin_text_search_does_not_treat_text_as_integer_id(self):
        response = self.client.get('/admin/payments/refund/', {'q': 'chen'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.refund.refund_no)

    def test_refund_admin_numeric_search_matches_user_id(self):
        response = self.client.get('/admin/payments/refund/', {'q': str(self.user.id)})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.refund.refund_no)
