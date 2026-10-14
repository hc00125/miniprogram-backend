"""Run explicitly with settings_gifts_pg_test; never treat SQLite as lock proof."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.db import connection, close_old_connections
from django.test import TransactionTestCase
from django.core.exceptions import ValidationError
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.gifts.models import Gift, PlayerGiftConfig, PlayerGiftConfigAudit
from apps.gifts.services.configuration import initialize_configs, update_config


class ConfigurationConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, 'postgresql', 'This test requires isolated PostgreSQL')
        self.actor = get_user_model().objects.create_superuser('operator', password='test')
        self.player = Player.objects.create(name='p', player_type=PlayerType.objects.create(name='t', priority=1))
        self.gift = Gift.objects.create(name='g', price_diamonds=1)

    def test_concurrent_initialization_creates_one_config_and_audit(self):
        barrier = Barrier(2)
        def worker(_):
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.actor.pk)
                barrier.wait(timeout=10)
                return initialize_configs(actor=actor, players=[self.player], gifts=[self.gift], reason='parallel')
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, range(2)))
        self.assertEqual(sorted(results), [0, 1])
        self.assertEqual(PlayerGiftConfig.objects.count(), 1)
        self.assertEqual(PlayerGiftConfigAudit.objects.count(), 1)

    def test_concurrent_edit_requires_version_reconfirmation(self):
        initialize_configs(actor=self.actor, players=[self.player], gifts=[self.gift], reason='initial')
        config = PlayerGiftConfig.objects.get()
        barrier = Barrier(2)
        def worker(rate):
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.actor.pk)
                barrier.wait(timeout=10)
                try:
                    update_config(actor=actor, config_id=config.pk, commission_rate=rate,
                                  is_enabled=True, reason='parallel', expected_version=1)
                    return 'saved'
                except ValidationError:
                    return 'changed'
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, (Decimal('10'), Decimal('20'))))
        self.assertEqual(sorted(results), ['changed', 'saved'])
        self.assertEqual(PlayerGiftConfigAudit.objects.count(), 2)
        config.refresh_from_db()
        self.assertEqual(config.version, 2)
