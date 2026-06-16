from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.catalog.models import Package, PlayerType
from apps.orders.boss_views import cancel_order, order_detail, rate_player, self_confirm_payment
from apps.orders.models import Order, OrderPlayer, OrderStatusLog, Rating
from apps.payments.models import Payment
from apps.players.models import Player


class BossOrderPermissionTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.owner = User.objects.create_user(username='owner', password='pass')
        self.other = User.objects.create_user(username='other', password='pass')
        self.admin = User.objects.create_user(username='admin', password='pass', is_staff=True)
        self.package = Package.objects.create(name='测试套餐', player_count=1, base_price=100)
        self.player_type = PlayerType.objects.create(name='普通', priority=1)
        self.player = Player.objects.create(name='陪玩A', player_type=self.player_type)

    def create_order(self, status=Order.STATUS_WAITING, paid=False, order_no='ORDER001'):
        return Order.objects.create(
            order_no=order_no,
            boss_user=self.owner,
            boss_wechat='boss_wechat',
            package=self.package,
            required_players=1,
            total_price_per_hour=100,
            total_amount=100,
            status=status,
            paid=paid,
        )

    def auth_get(self, view, user, *args):
        request = self.factory.get('/test')
        force_authenticate(request, user=user)
        return view(request, *args)

    def auth_post(self, view, user, data, *args):
        request = self.factory.post('/test', data, format='json')
        force_authenticate(request, user=user)
        return view(request, *args)

    def test_order_owner_can_view_detail(self):
        order = self.create_order()
        response = self.auth_get(order_detail, self.owner, order.order_no)
        self.assertEqual(response.status_code, 200)

    def test_other_user_cannot_view_detail(self):
        order = self.create_order()
        response = self.auth_get(order_detail, self.other, order.order_no)
        self.assertEqual(response.status_code, 403)

    def test_admin_can_view_detail(self):
        order = self.create_order()
        response = self.auth_get(order_detail, self.admin, order.order_no)
        self.assertEqual(response.status_code, 200)

    @override_settings(ENABLE_MOCK_PAYMENT=True)
    def test_owner_can_cancel_and_close_unpaid_payment(self):
        order = self.create_order(status=Order.STATUS_WAITING)
        payment = Payment.objects.create(
            payment_no='PAY001',
            order=order,
            channel='wechat',
            scene='jsapi',
            amount=100,
            status='paying',
        )
        response = self.auth_post(cancel_order, self.owner, {'reason': '取消'}, order.order_no)
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        self.assertEqual(payment.status, 'closed')

    def test_other_user_cannot_cancel_order(self):
        order = self.create_order(status=Order.STATUS_WAITING)
        response = self.auth_post(cancel_order, self.other, {'reason': '取消'}, order.order_no)
        self.assertEqual(response.status_code, 403)

    @override_settings(DEBUG=False, ENABLE_MOCK_PAYMENT=False, ENABLE_DEV_OPENID_LOGIN=False)
    def test_normal_user_cannot_self_confirm_payment_in_production(self):
        order = self.create_order(status=Order.STATUS_PENDING_PAYMENT)
        response = self.auth_post(self_confirm_payment, self.owner, {'actual_amount': 100}, order.order_no)
        self.assertEqual(response.status_code, 403)

    @override_settings(DEBUG=False, ENABLE_MOCK_PAYMENT=False, ENABLE_DEV_OPENID_LOGIN=False)
    def test_admin_can_self_confirm_payment(self):
        order = self.create_order(status=Order.STATUS_PENDING_PAYMENT)
        response = self.auth_post(self_confirm_payment, self.admin, {'actual_amount': 100}, order.order_no)
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertTrue(order.paid)
        self.assertEqual(order.status, Order.STATUS_COMPLETED)
        self.assertTrue(OrderStatusLog.objects.filter(order=order, reason='手动确认支付').exists())

    def test_owner_can_rate_paid_completed_order(self):
        order = self.create_order(status=Order.STATUS_COMPLETED, paid=True)
        OrderPlayer.objects.create(order=order, player=self.player)
        response = self.auth_post(rate_player, self.owner, {'player_id': self.player.id, 'rating': 5}, order.order_no)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Rating.objects.filter(order=order, player=self.player).exists())

    def test_unpaid_order_cannot_be_rated(self):
        order = self.create_order(status=Order.STATUS_COMPLETED, paid=False)
        OrderPlayer.objects.create(order=order, player=self.player)
        response = self.auth_post(rate_player, self.owner, {'player_id': self.player.id, 'rating': 5}, order.order_no)
        self.assertEqual(response.status_code, 400)

    def test_other_user_cannot_rate_order(self):
        order = self.create_order(status=Order.STATUS_COMPLETED, paid=True)
        OrderPlayer.objects.create(order=order, player=self.player)
        response = self.auth_post(rate_player, self.other, {'player_id': self.player.id, 'rating': 5}, order.order_no)
        self.assertEqual(response.status_code, 403)
