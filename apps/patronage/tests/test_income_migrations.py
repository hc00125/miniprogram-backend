"""Upgrade populated v1 patronage rows without changing any old snapshot."""
from datetime import timedelta
from decimal import Decimal
from django.db import connection, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class PatronageIncomeMigrationTests(TransactionTestCase):
    def test_v1_upgrade_preserves_purchase_and_legacy_grant_without_backfill_income(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        previous = [('patronage', '0005_purchase_snapshot_immutable')]
        executor.migrate(previous)
        try:
            old = executor.loader.project_state(previous).apps
            user = old.get_model('auth', 'User').objects.create(username='migration-boss')
            kind = old.get_model('catalog', 'PlayerType').objects.create(name='migration-type', priority=1)
            player = old.get_model('players', 'Player').objects.create(name='migration-player', player_type_id=kind.pk)
            Purchase = old.get_model('patronage', 'PatronagePurchase')
            Grant = old.get_model('patronage', 'CrownGrant')
            at = timezone.now()
            purchase = Purchase.objects.create(boss_id=user.pk, player_id=player.pk,
                player_name=player.name, package_code='year', package_name='历史年冠',
                amount_yuan=Decimal('188.00'), commission_rate=Decimal('.25'),
                platform_amount_yuan=Decimal('47'), player_amount_yuan=Decimal('141'),
                price_version='f'*64, config_snapshot={'historical': 'must-remain'},
                idempotency_key='historical-key', payment_status='paid', paid_at=at)
            grant = Grant.objects.create(purchase_id=purchase.pk, source='purchase', package_name='历史年冠',
                starts_at=at, expires_at=at+timedelta(days=730))
            purchase_before = Purchase.objects.values().get(pk=purchase.pk)
            grant_before = Grant.objects.values().get(pk=grant.pk)
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            from apps.patronage.models import PatronagePurchase, CrownGrant, PatronageEarning
            purchase_after = PatronagePurchase.objects.values().get(pk=purchase.pk)
            grant_after = CrownGrant.objects.values().get(pk=grant.pk)
            self.assertEqual({key: purchase_after[key] for key in purchase_before}, purchase_before)
            self.assertEqual({key: grant_after[key] for key in grant_before}, grant_before)
            self.assertIsNone(purchase_after['attempt_id'])
            self.assertIsNone(grant_after['duration_days'])
            self.assertIsNone(grant_after['extension_starts_at'])
            self.assertFalse(PatronageEarning.objects.exists())
            with self.assertRaises(IntegrityError), transaction.atomic():
                PatronagePurchase.objects.filter(pk=purchase.pk).update(config_snapshot={})
        finally:
            MigrationExecutor(connection).migrate(latest)
