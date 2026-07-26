from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.catalog.models import Package, PlayerType
from apps.payments.models import Payment
from apps.players.models import Player

from .models import Order, OrderPlayer, OrderStatusLog
from .room_entry_requeue import expire_due_room_entries, start_room_entry_window
from .services import can_player_grab_order, create_order, grab_order


class RoomEntryRequeueTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_user(username='room-entry-boss')
        self.type = PlayerType.objects.create(name='补位测试陪玩', priority=991)
        self.package = Package.objects.create(
            name='进入房间超时补位测试',
            base_price=20,
            player_count=1,
            is_active=True,
        )
        self.first_user = User.objects.create_user(username='room-entry-first')
        self.first_player = Player.objects.create(
            user=self.first_user,
            name='超时陪玩',
            player_type=self.type,
            status=Player.STATUS_APPROVED,
            is_online=True,
            total_orders=0,
        )
        self.replacement_user = User.objects.create_user(username='room-entry-replacement')
        self.replacement = Player.objects.create(
            user=self.replacement_user,
            name='补位陪玩',
            player_type=self.type,
            status=Player.STATUS_APPROVED,
            is_online=True,
            total_orders=0,
        )

    def create_pending_order(self):
        order = create_order({
            'boss_wechat': 'room-entry-openid',
            'game_id': 'ROOM-ENTRY-TEST',
            'package_id': self.package.id,
            'quantity': 1,
            'required_players': 1,
            'booked_hours': 1,
        }, self.boss)
        order = grab_order(order.order_no, self.first_player, self.first_user)
        relation = order.order_players.get(player=self.first_player)
        self.assertIsNone(relation.room_join_deadline)
        return order

    def create_paid_ready_order(self):
        order = self.create_pending_order()
        paid_at = timezone.now()
        order.paid = True
        order.status = Order.STATUS_READY_TO_START
        order.payment_confirmed_at = paid_at
        order.save(update_fields=['paid', 'status', 'payment_confirmed_at'])
        start_room_entry_window(order, paid_at)
        return order

    def test_verified_payment_signal_starts_timer_before_order_row_updates(self):
        order = self.create_pending_order()
        paid_at = timezone.now()
        payment = Payment.objects.create(
            payment_no='ROOMENTRY-PAY-1',
            order=order,
            channel='wechat_virtual',
            scene='miniprogram',
            amount=20,
            status='created',
        )

        payment.status = 'paid'
        payment.paid_at = paid_at
        payment.save(update_fields=['status', 'paid_at'])

        relation = order.order_players.get(player=self.first_player)
        self.assertIsNotNone(relation.room_join_deadline)
        self.assertEqual(relation.room_join_deadline, paid_at + timedelta(minutes=10))

    def test_timeout_releases_player_and_reopens_paid_order(self):
        order = self.create_paid_ready_order()
        relation = order.order_players.get(player=self.first_player)
        OrderPlayer.objects.filter(pk=relation.pk).update(
            room_join_deadline=timezone.now() - timedelta(seconds=1)
        )

        updated = expire_due_room_entries()

        self.assertEqual(updated, 1)
        order.refresh_from_db()
        self.first_player.refresh_from_db()
        self.assertTrue(order.paid)
        self.assertEqual(order.status, Order.STATUS_WAITING)
        self.assertFalse(order.order_players.filter(player=self.first_player).exists())
        self.assertEqual(self.first_player.total_orders, 0)
        self.assertFalse(can_player_grab_order(order, self.first_player))
        self.assertTrue(can_player_grab_order(order, self.replacement))
        self.assertTrue(OrderStatusLog.objects.filter(
            order=order,
            operator=self.first_user,
            reason__contains='视为拒单',
        ).exists())

    def test_replacement_does_not_trigger_second_payment(self):
        order = self.create_paid_ready_order()
        relation = order.order_players.get(player=self.first_player)
        OrderPlayer.objects.filter(pk=relation.pk).update(
            room_join_deadline=timezone.now() - timedelta(seconds=1)
        )
        expire_due_room_entries()

        result = grab_order(order.order_no, self.replacement, self.replacement_user)

        result.refresh_from_db()
        replacement_relation = result.order_players.get(player=self.replacement)
        self.assertTrue(result.paid)
        self.assertEqual(result.status, Order.STATUS_READY_TO_START)
        self.assertIsNotNone(replacement_relation.room_join_deadline)
        self.assertEqual(replacement_relation.room_join_status, OrderPlayer.ROOM_ENTRY_PENDING)
