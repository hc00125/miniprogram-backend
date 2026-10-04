from concurrent.futures import ThreadPoolExecutor
from threading import Event
from django.db import connections, transaction
from django.test import TransactionTestCase
from django.contrib.auth import get_user_model
from rest_framework.exceptions import ValidationError
from apps.catalog.models import PlayerType, Package
from apps.orders.models import Order, OrderPlayer
from apps.players.models import Player
from apps.players.archive import archive_players


class ArchiveRaceTests(TransactionTestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user('race-admin')
        self.player = Player.objects.create(name='race-player', player_type=PlayerType.objects.create(name='race-type', priority=1))
        package = Package.objects.create(name='race-package', base_price=10, player_count=1)
        self.order = Order.objects.create(order_no='archive-race', package=package, boss_wechat='fixture', required_players=1, total_price_per_hour=10)

    def test_archive_wins_and_stale_order_insert_waits_then_rejects(self):
        started = Event()
        def insert():
            try:
                started.set()
                OrderPlayer.objects.create(order_id=self.order.pk, player_id=self.player.pk)
                return 'inserted'
            except ValidationError as exc:
                return str(exc.detail['code'])
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                archive_players([self.player.pk], actor=self.actor, reason='race')
                future = pool.submit(insert)
                self.assertTrue(started.wait(3))
                # A second database connection may not commit a new assignment
                # until archive's row lock is released.
                from concurrent.futures import TimeoutError
                with self.assertRaises(TimeoutError):
                    future.result(timeout=0.15)
            self.assertEqual(future.result(timeout=5), 'PLAYER_ARCHIVED')
        self.assertFalse(OrderPlayer.objects.exists())

    def test_assignment_wins_archive_waits_and_keeps_history(self):
        started = Event()
        def archive():
            try:
                started.set()
                archive_players([self.player.pk], actor=self.actor, reason='race')
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                relation = OrderPlayer.objects.create(order=self.order, player=self.player)
                future = pool.submit(archive)
                self.assertTrue(started.wait(3))
                from concurrent.futures import TimeoutError
                with self.assertRaises(TimeoutError):
                    future.result(timeout=0.15)
            future.result(timeout=5)
        self.player.refresh_from_db()
        self.assertTrue(self.player.is_archived)
        self.assertTrue(OrderPlayer.objects.filter(pk=relation.pk).exists())
