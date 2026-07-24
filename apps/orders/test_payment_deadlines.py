from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.catalog.models import Package, PackageSpec, PlayerType
from apps.players.models import Player

from .cancel_signals import ORDER_PLAYER_CANCELLED_STATUS
from .models import Order, OrderStatusLog
from .payment_deadlines import (
    PAYMENT_TIMEOUT_REASON,
    ensure_payment_window_open,
    expire_due_unpaid_order,
    expire_due_unpaid_orders,
    payment_deadline_payload,
)
from .services import create_order, grab_order


class PaymentDeadlineTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_user(username='payment-deadline-boss')
        self.player_user = User.objects.create_user(username='payment-deadline-player')
        self.player_type = PlayerType.objects.create(name='支付时限娱乐陪', priority=951)
        self.package = Package.objects.create(
            name='支付时限测试商品',
            base_price=15,
            player_count=1,
            is_active=True,
        )
        self.spec = PackageSpec.objects.create(
            package=self.package,
            name='支付时限测试规格',
            price=15,
            required_player_type=self.player_type,
            is_active=True,
        )
        self.player = Player.objects.create(
            user=self.player_user,
            name='支付时限测试陪玩',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
            is_online=True,
            total_orders=0,
        )

    def create_pending_payment_order(self):
        order = create_order({
            'boss_wechat': 'payment-deadline-openid',
            'game_id': 'PAYMENT-DEADLINE-ROOM',
            'package_id': self.package.id,
            'spec_id': self.spec.id,
            'quantity': 1,
            'required_players': 1,
            'booked_hours': 1,
        }, self.boss)
        return grab_order(order.order_no, self.player, self.player_user)

    def backdate_payment_window(self, order, minutes):
        started_at = timezone.now() - timedelta(minutes=minutes)
        log = (
            OrderStatusLog.objects
            .filter(order=order, to_status=Order.STATUS_PENDING_PAYMENT)
            .order_by('-created_at')
            .first()
        )
        self.assertIsNotNone(log)
        OrderStatusLog.objects.filter(pk=log.pk).update(created_at=started_at)
        return started_at

    def test_pending_payment_payload_counts_from_lineup_completion(self):
        order = self.create_pending_payment_order()
        started_at = self.backdate_payment_window(order, 3)

        payload = payment_deadline_payload(order, now=started_at + timedelta(minutes=3))

        self.assertEqual(payload['payment_timeout_minutes'], 10)
        self.assertEqual(payload['payment_remaining_seconds'], 7 * 60)
        self.assertEqual(payload['payment_deadline_at'], started_at + timedelta(minutes=10))

    def test_due_order_is_cancelled_and_player_is_released(self):
        order = self.create_pending_payment_order()
        self.backdate_payment_window(order, 11)

        expired = expire_due_unpaid_order(order, now=timezone.now(), verify_remote=False)

        self.assertTrue(expired)
        order.refresh_from_db()
        relation = order.order_players.get(player=self.player)
        self.player.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        self.assertEqual(order.cancel_reason, PAYMENT_TIMEOUT_REASON)
        self.assertEqual(relation.status, ORDER_PLAYER_CANCELLED_STATUS)
        self.assertIsNone(relation.room_join_deadline)
        self.assertEqual(self.player.total_orders, 0)

    def test_order_inside_window_is_not_cancelled(self):
        order = self.create_pending_payment_order()
        self.backdate_payment_window(order, 9)

        expired = expire_due_unpaid_orders(now=timezone.now())

        self.assertEqual(expired, 0)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PENDING_PAYMENT)

    def test_payment_creation_guard_cancels_expired_order(self):
        order = self.create_pending_payment_order()
        self.backdate_payment_window(order, 11)

        with self.assertRaises(ValidationError) as context:
            ensure_payment_window_open(order.order_no, self.boss)

        self.assertIn('支付时间已超过10分钟', str(context.exception.detail))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
