from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.catalog.models import Package, PlayerType
from apps.payments.models import Payment
from apps.players.models import Player

from .models import Order, OrderPlayer
from .room_entry_requeue import expire_due_room_entries, start_room_entry_window
from .services import create_order, grab_order


class RoomEntryRequeueTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_user(username='room-entry-boss')
        self.type = PlayerType.objects.create(name='进入房间测试陪玩', priority=991)
        self.package = Package.objects.create(
            name='进入房间确认测试',
            base_price=20,
            player_count=1,
            is_active=True,
        )
        self.first_user = User.objects.create_user(username='room-entry-first')
        self.first_player = Player.objects.create(
            user=self.first_user,
            name='房间确认陪玩',
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

    def test_verified_payment_signal_opens_confirmation_without_deadline(self):
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
        self.assertIsNone(relation.room_join_deadline)
        self.assertEqual(relation.room_join_status, OrderPlayer.ROOM_ENTRY_PENDING)

    def test_expiration_task_never_releases_player(self):
        order = self.create_paid_ready_order()
        relation = order.order_players.get(player=self.first_player)

        # 模拟旧版本遗留的已过期截止时间；新规则必须忽略它。
        OrderPlayer.objects.filter(pk=relation.pk).update(
            room_join_deadline=timezone.now() - timedelta(minutes=30),
            room_join_status=OrderPlayer.ROOM_ENTRY_OVERDUE,
        )

        updated = expire_due_room_entries()

        self.assertEqual(updated, 0)
        self.assertTrue(order.order_players.filter(player=self.first_player).exists())
        self.first_player.refresh_from_db()
        self.assertEqual(self.first_player.total_orders, 1)

    def test_paid_order_player_created_later_also_has_no_deadline(self):
        order = self.create_pending_order()
        order.paid = True
        order.status = Order.STATUS_READY_TO_START
        order.save(update_fields=['paid', 'status'])

        relation = order.order_players.get(player=self.first_player)
        OrderPlayer.objects.filter(pk=relation.pk).delete()
        replacement = OrderPlayer.objects.create(
            order=order,
            player=self.first_player,
            status='已接单',
        )
        replacement.refresh_from_db()

        self.assertIsNone(replacement.room_join_deadline)
        self.assertEqual(replacement.room_join_status, OrderPlayer.ROOM_ENTRY_PENDING)
