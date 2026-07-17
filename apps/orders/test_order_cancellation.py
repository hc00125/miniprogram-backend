from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.exceptions import ValidationError

from apps.catalog.models import Package, PackageSpec, PlayerType
from apps.players.models import Player

from .cancel_signals import ORDER_PLAYER_CANCELLED_STATUS
from .models import Order
from .services import cancel_order, create_order, grab_order


class UnpaidOrderCancellationTests(TestCase):
    """待接单/待支付订单可取消；已付款订单必须走退款流程。"""

    def setUp(self):
        self.boss = User.objects.create_user(username='cancel-order-boss')
        self.player_user = User.objects.create_user(username='cancel-order-player')
        self.player_type = PlayerType.objects.create(name='取消测试娱乐陪', priority=901)
        self.package = Package.objects.create(
            name='取消测试单人陪玩',
            base_price=15,
            player_count=1,
            is_active=True,
        )
        self.spec = PackageSpec.objects.create(
            package=self.package,
            name='取消测试娱乐陪规格',
            price=15,
            required_player_type=self.player_type,
            is_active=True,
        )
        self.player = Player.objects.create(
            user=self.player_user,
            name='取消测试陪玩',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
            is_online=True,
            total_orders=0,
        )

    def create_order(self):
        return create_order({
            'boss_wechat': 'cancel-order-openid',
            'game_id': 'CANCEL-ROOM',
            'package_id': self.package.id,
            'spec_id': self.spec.id,
            'quantity': 1,
            'required_players': 1,
            'booked_hours': 1,
        }, self.boss)

    def test_boss_can_cancel_pending_payment_and_release_accepted_player(self):
        order = self.create_order()
        order = grab_order(order.order_no, self.player, self.player_user)
        relation = order.order_players.get(player=self.player)
        self.player.refresh_from_db()

        self.assertEqual(order.status, Order.STATUS_PENDING_PAYMENT)
        self.assertIsNotNone(relation.room_join_deadline)
        self.assertEqual(self.player.total_orders, 1)

        cancel_order(order, '老板在支付前主动取消', self.boss)

        order.refresh_from_db()
        relation.refresh_from_db()
        self.player.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        self.assertEqual(order.cancel_reason, '老板在支付前主动取消')
        self.assertEqual(relation.status, ORDER_PLAYER_CANCELLED_STATUS)
        self.assertIsNone(relation.room_join_deadline)
        self.assertEqual(self.player.total_orders, 0)

        # 后续保存已取消订单不能重复扣减陪玩接单次数。
        order.boss_note = '取消后补充审计备注'
        order.save(update_fields=['boss_note'])
        self.player.refresh_from_db()
        self.assertEqual(self.player.total_orders, 0)

    def test_waiting_order_without_players_can_be_cancelled(self):
        order = self.create_order()

        cancel_order(order, '下错套餐', self.boss)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        self.assertEqual(order.order_players.count(), 0)

    def test_paid_order_cannot_be_cancelled_directly(self):
        order = self.create_order()
        order.status = Order.STATUS_READY_TO_START
        order.paid = True
        order.save(update_fields=['status', 'paid'])

        with self.assertRaises(ValidationError):
            cancel_order(order, '尝试取消已付款订单', self.boss)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_READY_TO_START)
        self.assertTrue(order.paid)

    def test_paid_order_manual_status_change_does_not_release_player_or_revert_statistics(self):
        order = self.create_order()
        order = grab_order(order.order_no, self.player, self.player_user)
        relation = order.order_players.get(player=self.player)
        original_deadline = relation.room_join_deadline

        # 防御性场景：后台误把已支付订单状态改成已取消，也不能触发“未支付取消”的释放逻辑。
        order.paid = True
        order.status = Order.STATUS_CANCELLED
        order.save(update_fields=['paid', 'status'])

        relation.refresh_from_db()
        self.player.refresh_from_db()
        self.assertNotEqual(relation.status, ORDER_PLAYER_CANCELLED_STATUS)
        self.assertEqual(relation.room_join_deadline, original_deadline)
        self.assertEqual(self.player.total_orders, 1)
