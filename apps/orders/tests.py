from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.catalog.models import Package, PlayerType
from apps.orders.boss_views import cancel_order, order_detail, rate_player, self_confirm_payment
from apps.orders.models import Order, OrderItem, OrderPlayer, OrderStatusLog, Rating
from apps.orders.renewals import create_renewal_order
from apps.orders.serializers import BossOrderDetailSerializer
from apps.orders.services import complete_order, grab_order, start_timer
from apps.payments.models import Payment
from apps.payments.services import mark_payment_paid
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
        order = self.create_order(status=Order.STATUS_PENDING_PAYMENT)
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
        self.assertEqual(order.status, Order.STATUS_READY_TO_START)
        self.assertTrue(OrderStatusLog.objects.filter(order=order, reason__contains='等待陪玩开打').exists())

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


class PrepaidOrderFlowTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='flow-owner')
        self.player_user = User.objects.create_user(username='flow-player')
        self.package = Package.objects.create(name='流程测试套餐', player_count=1, base_price=15)
        self.player_type = PlayerType.objects.create(name='流程陪玩', priority=1)
        self.player = Player.objects.create(
            user=self.player_user,
            name='流程陪玩A',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
        )
        self.order = Order.objects.create(
            order_no='FLOW000001',
            boss_user=self.owner,
            boss_wechat='flow-boss',
            package=self.package,
            required_players=1,
            total_price_per_hour=15,
            total_amount=15,
            status=Order.STATUS_WAITING,
        )

    def test_full_lineup_moves_order_to_pending_payment(self):
        grab_order(self.order.order_no, self.player, self.player_user)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_PENDING_PAYMENT)
        self.assertIsNone(self.order.start_time)

    def test_paid_order_can_start_and_complete(self):
        OrderPlayer.objects.create(order=self.order, player=self.player)
        self.order.status = Order.STATUS_READY_TO_START
        self.order.paid = True
        self.order.save(update_fields=['status', 'paid'])

        started = start_timer(self.order, self.player, self.player_user)
        self.assertEqual(started.status, Order.STATUS_IN_PROGRESS)
        self.assertIsNotNone(started.timer_started_at)

        completed = complete_order(started, self.player, self.player_user)
        self.assertEqual(completed.status, Order.STATUS_COMPLETED)
        self.assertIsNotNone(completed.end_time)
        self.assertTrue(completed.paid)


class RenewalOrderTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='renew-owner')
        self.other = User.objects.create_user(username='renew-other')
        self.player_user = User.objects.create_user(username='renew-player')
        self.package = Package.objects.create(name='四套四弹', player_count=1, base_price=15)
        self.player_type = PlayerType.objects.create(name='续单陪玩', priority=1)
        self.player = Player.objects.create(
            user=self.player_user,
            name='续单陪玩A',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
        )
        self.order = Order.objects.create(
            order_no='RENEWROOT01',
            boss_user=self.owner,
            boss_wechat='renew-boss',
            game_id='game-001',
            package=self.package,
            package_name_snapshot='四套四弹',
            required_players=1,
            total_price_per_hour=15,
            total_amount=15,
            status=Order.STATUS_IN_PROGRESS,
            paid=True,
            booked_hours=1,
            order_type=Order.ORDER_TYPE_NORMAL,
        )
        OrderItem.objects.create(
            order=self.order,
            package=self.package,
            package_name='四套四弹',
            unit_price=15,
            quantity=1,
            amount=15,
        )
        OrderPlayer.objects.create(order=self.order, player=self.player)

    def test_create_renewal_clones_item_players_and_duration(self):
        renewal, created = create_renewal_order(self.order.order_no, self.owner, units=2)

        self.assertTrue(created)
        self.assertEqual(renewal.parent_order, self.order)
        self.assertEqual(renewal.order_type, Order.ORDER_TYPE_RENEWAL)
        self.assertEqual(renewal.renewal_index, 1)
        self.assertEqual(renewal.status, Order.STATUS_PENDING_PAYMENT)
        self.assertEqual(renewal.booked_hours, 2)
        self.assertEqual(renewal.total_amount, 30)
        self.assertEqual(renewal.items.get().quantity, 2)
        self.assertTrue(renewal.order_players.filter(player=self.player).exists())

    def test_existing_unpaid_renewal_is_reused(self):
        first, first_created = create_renewal_order(self.order.order_no, self.owner, units=1)
        second, second_created = create_renewal_order(self.order.order_no, self.owner, units=2)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.order.renewal_orders.count(), 1)

    def test_other_user_cannot_create_renewal(self):
        with self.assertRaises(Exception):
            create_renewal_order(self.order.order_no, self.other, units=1)

    def test_paid_renewal_is_completed_and_merged_in_serializer(self):
        renewal, _ = create_renewal_order(self.order.order_no, self.owner, units=2)
        payment = Payment.objects.create(
            payment_no='PAYRENEW01',
            order=renewal,
            channel='wechat_virtual',
            scene='short_series_goods',
            amount=30,
            status='paying',
        )

        mark_payment_paid(payment, third_trade_no='WXRENEW01', payload={'ok': True})

        renewal.refresh_from_db()
        self.assertTrue(renewal.paid)
        self.assertEqual(renewal.status, Order.STATUS_COMPLETED)
        data = BossOrderDetailSerializer(self.order).data
        self.assertEqual(data['renewal_count'], 1)
        self.assertEqual(data['renewal_booked_hours'], 2.0)
        self.assertEqual(data['total_booked_hours'], 3.0)
        self.assertEqual(data['renewal_paid_amount'], 30.0)

    def test_service_cannot_complete_with_unpaid_renewal(self):
        create_renewal_order(self.order.order_no, self.owner, units=1)
        with self.assertRaises(ValidationError):
            complete_order(self.order, self.player, self.player_user)
