from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.catalog.models import Package, PlayerType
from apps.orders.cancellation_models import OrderReplacementState, PlayerDiscipline
from apps.orders.matching import extend_matching, matching_payload
from apps.orders.models import Order, OrderPlayer
from apps.orders.player_cancellations import cancel_player_order
from apps.orders.replacement_fixes import replacement_payload
from apps.players.models import Player


class PublicMatchingFlowTests(TestCase):
    def setUp(self):
        self.player_type = PlayerType.objects.create(name='全能陪测试', priority=10, price_extra=0)
        self.package = Package.objects.create(
            name='活动抢单测试商品',
            base_price=30,
            player_count=3,
            is_active=True,
        )
        self.player = Player.objects.create(
            name='匹配测试陪玩A',
            player_type=self.player_type,
            is_online=True,
            status=Player.STATUS_APPROVED,
        )

    def make_waiting_order(self, order_no='MATCHTEST000000001'):
        order = Order.objects.create(
            order_no=order_no,
            boss_wechat='boss-matching-test',
            package=self.package,
            required_players=3,
            booked_hours=1,
            total_price_per_hour=30,
            total_amount=30,
            status=Order.STATUS_WAITING,
            paid=False,
            fulfillment_mode=Order.FULFILLMENT_MODE_PUBLIC,
        )
        relation = OrderPlayer.objects.create(order=order, player=self.player, is_designated=False)
        return order, relation

    def test_unpaid_public_cancel_returns_to_normal_matching_without_replacement(self):
        order, _ = self.make_waiting_order()

        result = cancel_player_order(order.order_no, self.player, '临时无法继续等待')

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_WAITING)
        self.assertFalse(OrderPlayer.objects.filter(order=order, player=self.player).exists())
        self.assertFalse(
            OrderReplacementState.objects.filter(
                order=order,
                status=OrderReplacementState.STATUS_OPEN,
            ).exists()
        )
        self.assertFalse(result['replacement']['active'])

    def test_player_can_exit_without_penalty_after_fifteen_minutes(self):
        order, relation = self.make_waiting_order('MATCHTEST000000002')
        OrderPlayer.objects.filter(pk=relation.pk).update(
            grab_time=timezone.now() - timedelta(minutes=16),
        )

        result = cancel_player_order(order.order_no, self.player, '等待组队时间过长')

        self.player.refresh_from_db()
        discipline = PlayerDiscipline.objects.get(player=self.player)
        self.assertTrue(result['no_fault'])
        self.assertFalse(result['used_free_chance'])
        self.assertEqual(float(result['fine_rmb']), 0)
        self.assertTrue(self.player.is_online)
        self.assertFalse(discipline.suspended_until and discipline.suspended_until > timezone.now())

    def test_matching_window_requires_decision_and_can_be_extended(self):
        order, _ = self.make_waiting_order('MATCHTEST000000003')
        window = order.matching_window
        window.started_at = timezone.now() - timedelta(minutes=31)
        window.deadline_at = timezone.now() - timedelta(minutes=1)
        window.save(update_fields=['started_at', 'deadline_at', 'updated_at'])

        before = matching_payload(order)
        self.assertTrue(before['decision_required'])
        self.assertTrue(before['players_can_exit_without_penalty'])

        extend_matching(order)
        order.refresh_from_db()
        after = matching_payload(order)
        self.assertFalse(after['decision_required'])
        self.assertGreater(after['remaining_seconds'], 29 * 60)
        self.assertEqual(after['extension_count'], 1)

    def test_replacement_payload_recalculates_real_missing_slots(self):
        order, relation = self.make_waiting_order('MATCHTEST000000004')
        order.paid = True
        order.status = Order.STATUS_READY_TO_START
        order.save(update_fields=['paid', 'status'])
        second = Player.objects.create(
            name='匹配测试陪玩B',
            player_type=self.player_type,
            is_online=True,
            status=Player.STATUS_APPROVED,
        )
        OrderPlayer.objects.create(order=order, player=second)
        state = OrderReplacementState.objects.create(
            order=order,
            mode=OrderReplacementState.MODE_PUBLIC,
            status=OrderReplacementState.STATUS_OPEN,
            missing_slots=3,
            resume_status=Order.STATUS_READY_TO_START,
        )

        payload = replacement_payload(order)

        self.assertEqual(state.missing_slots, 3)
        self.assertEqual(payload['missing_slots'], 1)
