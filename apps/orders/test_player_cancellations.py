from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.catalog.models import Package, PlayerType
from apps.earnings.models import PlayerWallet
from apps.orders.cancellation_models import (
    OrderReplacementState,
    PlayerCancellationRecord,
    PlayerDiscipline,
)
from apps.orders.models import Order, OrderPlayer
from apps.orders.player_cancellations import cancel_player_order
from apps.orders.replacements import (
    accept_replacement_designation,
    reassign_replacement,
)
from apps.players.models import Player


class PlayerCancellationFlowTests(TestCase):
    def setUp(self):
        self.player_type = PlayerType.objects.create(
            name='技术陪',
            priority=10,
            price_extra=0,
        )
        self.package = Package.objects.create(
            name='四套四弹测试套餐',
            base_price=25,
            player_count=1,
            is_active=True,
        )
        self.player = Player.objects.create(
            name='测试打手A',
            player_type=self.player_type,
            is_online=True,
            status=Player.STATUS_APPROVED,
        )

    def make_order(
        self,
        order_no,
        *,
        status=Order.STATUS_READY_TO_START,
        paid=True,
        fulfillment_mode=Order.FULFILLMENT_MODE_PUBLIC,
        target_player=None,
        booked_hours=2,
        designated=False,
        required_players=1,
    ):
        order = Order.objects.create(
            order_no=order_no,
            boss_wechat='boss-test',
            package=self.package,
            required_players=required_players,
            booked_hours=booked_hours,
            total_price_per_hour=25,
            total_amount=25 * booked_hours,
            status=status,
            paid=paid,
            fulfillment_mode=fulfillment_mode,
            target_player=target_player,
            target_player_name_snapshot=target_player.name if target_player else '',
        )
        OrderPlayer.objects.create(
            order=order,
            player=self.player,
            is_designated=designated,
        )
        return order

    def test_first_pre_join_cancel_is_free_and_reopens_paid_public_slot(self):
        order = self.make_order('CANCELTEST000000001')

        result = cancel_player_order(order.order_no, self.player, '临时身体不适')

        order.refresh_from_db()
        self.player.refresh_from_db()
        record = PlayerCancellationRecord.objects.get(order=order, player=self.player)
        replacement = OrderReplacementState.objects.get(order=order)
        discipline = PlayerDiscipline.objects.get(player=self.player)

        self.assertTrue(result['used_free_chance'])
        self.assertEqual(record.fine_rmb, Decimal('0.00'))
        self.assertEqual(order.status, Order.STATUS_READY_TO_START)
        self.assertEqual(replacement.mode, OrderReplacementState.MODE_PUBLIC)
        self.assertEqual(replacement.missing_slots, 1)
        self.assertFalse(OrderPlayer.objects.filter(order=order, player=self.player).exists())
        self.assertFalse(self.player.is_online)
        self.assertGreater(discipline.suspended_until, timezone.now() + timedelta(hours=1, minutes=50))

    def test_paid_two_player_order_keeps_remaining_player_and_ready_status(self):
        order = self.make_order(
            'CANCELTEST000000006',
            required_players=2,
            status=Order.STATUS_READY_TO_START,
            paid=True,
        )
        leaving_player = Player.objects.create(
            name='测试打手C',
            player_type=self.player_type,
            is_online=True,
            status=Player.STATUS_APPROVED,
        )
        OrderPlayer.objects.create(order=order, player=leaving_player)

        cancel_player_order(order.order_no, leaving_player, '付款后临时退出')

        order.refresh_from_db()
        replacement = OrderReplacementState.objects.get(order=order)
        self.assertEqual(order.status, Order.STATUS_READY_TO_START)
        self.assertTrue(order.paid)
        self.assertEqual(order.order_players.count(), 1)
        self.assertTrue(order.order_players.filter(player=self.player).exists())
        self.assertFalse(order.order_players.filter(player=leaving_player).exists())
        self.assertEqual(replacement.status, OrderReplacementState.STATUS_OPEN)
        self.assertEqual(replacement.missing_slots, 1)
        self.assertEqual(replacement.resume_status, Order.STATUS_READY_TO_START)

    def test_second_pre_join_cancel_same_day_creates_twenty_yuan_debt(self):
        first = self.make_order('CANCELTEST000000002')
        cancel_player_order(first.order_no, self.player, '第一单临时取消')

        self.player.is_online = True
        self.player.save(update_fields=['is_online'])
        second = self.make_order('CANCELTEST000000003')
        result = cancel_player_order(second.order_no, self.player, '第二单仍无法履约')

        wallet = PlayerWallet.objects.get(player=self.player)
        record = PlayerCancellationRecord.objects.get(order=second, player=self.player)

        self.assertFalse(result['used_free_chance'])
        self.assertEqual(record.fine_rmb, Decimal('20.00'))
        self.assertEqual(record.fine_fish, Decimal('200.00'))
        self.assertEqual(record.debt_fish, Decimal('200.00'))
        self.assertEqual(wallet.debt_balance, Decimal('200.00'))

    def test_targeted_in_service_cancel_waits_for_boss_reassignment(self):
        order = self.make_order(
            'CANCELTEST000000004',
            status=Order.STATUS_IN_PROGRESS,
            fulfillment_mode=Order.FULFILLMENT_MODE_TARGETED,
            target_player=self.player,
            booked_hours=2,
            designated=True,
        )
        order.timer_started_at = timezone.now() - timedelta(minutes=35)
        order.save(update_fields=['timer_started_at'])

        result = cancel_player_order(order.order_no, self.player, '服务中突发断网')

        order.refresh_from_db()
        replacement = OrderReplacementState.objects.get(order=order)
        self.assertEqual(order.status, Order.STATUS_IN_PROGRESS)
        self.assertIsNone(order.target_player_id)
        self.assertEqual(replacement.mode, OrderReplacementState.MODE_TARGETED)
        self.assertEqual(replacement.status, OrderReplacementState.STATUS_OPEN)
        self.assertGreaterEqual(replacement.remaining_minutes, 84)
        self.assertLessEqual(replacement.remaining_minutes, 86)
        self.assertEqual(result['replacement']['mode'], 'targeted')

    def test_targeted_replacement_acceptance_keeps_original_order(self):
        order = self.make_order(
            'CANCELTEST000000005',
            status=Order.STATUS_IN_PROGRESS,
            fulfillment_mode=Order.FULFILLMENT_MODE_TARGETED,
            target_player=self.player,
            designated=True,
        )
        order.timer_started_at = timezone.now() - timedelta(minutes=10)
        order.save(update_fields=['timer_started_at'])
        cancel_player_order(order.order_no, self.player, '无法继续服务')

        replacement_player = Player.objects.create(
            name='测试打手B',
            player_type=self.player_type,
            is_online=True,
            status=Player.STATUS_APPROVED,
        )
        designation = reassign_replacement(order, replacement_player.id)
        accepted_order = accept_replacement_designation(order.order_no, replacement_player)

        designation.refresh_from_db()
        accepted_order.refresh_from_db()
        replacement = OrderReplacementState.objects.get(order=accepted_order)
        self.assertEqual(designation.status, 'accepted')
        self.assertEqual(accepted_order.status, Order.STATUS_IN_PROGRESS)
        self.assertTrue(OrderPlayer.objects.filter(order=accepted_order, player=replacement_player).exists())
        self.assertEqual(replacement.status, OrderReplacementState.STATUS_RESOLVED)
        self.assertEqual(replacement.missing_slots, 0)
