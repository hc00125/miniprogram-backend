"""Real PostgreSQL races; each worker uses its own connection and transaction."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import TransactionTestCase
from rest_framework.exceptions import APIException
from apps.orders import test_dispatch
from apps.orders.models import Order, OrderStatusLog
from apps.orders.services import grab_order, start_timer
from apps.dispatch.cancellations import cancel_dispatch


class CancellationRaceTests(TransactionTestCase):
    setUp = test_dispatch.DispatchTests.setUp
    post = test_dispatch.DispatchTests.post
    make_dispatch = test_dispatch.DispatchTests.make_dispatch

    def race(self, other, ready=False):
        from apps.catalog.models import PlayerType
        from apps.players.models import Player
        order, _, _ = self.make_dispatch()
        kind = PlayerType.objects.create(name='concurrency', priority=0)
        player = Player.objects.create(name='race-player', player_type=kind, status='approved', is_online=True)
        if ready:
            order = grab_order(order.order_no, player)
        barrier = Barrier(2)
        def worker(operation):
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.agent.pk)
                p = Player.objects.get(pk=player.pk)
                barrier.wait(timeout=10)
                if operation == 'cancel':
                    result = cancel_dispatch(order.order_no, {'reason':'并发取消测试'}, actor)
                elif operation == 'grab':
                    result = grab_order(order.order_no, p)
                else:
                    result = start_timer(Order.objects.get(pk=order.pk), p)
                return operation, result.status
            except APIException as exc:
                return operation, 'rejected'
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, action) for action in ('cancel', other)]
            results = [f.result(timeout=30) for f in futures]
        order.refresh_from_db(); player.refresh_from_db()
        return order, player, results

    def test_cancel_and_grab_share_parent_lock(self):
        order, player, result = self.race('grab')
        self.assertEqual(order.status, Order.STATUS_CANCELLED, result)
        self.assertFalse(order.order_players.exclude(status='订单已取消').exists())
        self.assertEqual(player.total_orders, 0)
        self.assertFalse(order.player_earnings.exists())

    def test_cancel_and_start_have_only_one_winner(self):
        order, player, result = self.race('start', ready=True)
        self.assertIn(order.status, [Order.STATUS_CANCELLED, Order.STATUS_IN_PROGRESS], result)
        if order.status == Order.STATUS_CANCELLED:
            self.assertIsNone(order.timer_started_at)
            self.assertIn(('start', 'rejected'), result)
            self.assertEqual(player.total_orders, 0)
        else:
            self.assertIsNotNone(order.timer_started_at)
            self.assertIsNone(order.canceled_at)
            self.assertIn(('cancel', 'rejected'), result)
        self.assertFalse(order.player_earnings.exists())

    def test_duplicate_cancel_has_one_audit_and_one_counter_decrement(self):
        order, player, result = self.race('cancel', ready=True)
        self.assertEqual(result, [('cancel', Order.STATUS_CANCELLED)] * 2)
        self.assertEqual(OrderStatusLog.objects.filter(order=order, to_status=Order.STATUS_CANCELLED).count(), 1)
        self.assertEqual(player.total_orders, 0)
