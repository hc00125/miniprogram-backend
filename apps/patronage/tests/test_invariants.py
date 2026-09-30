from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.test import TestCase
from django.utils import timezone
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.patronage.models import PatronagePurchase, PatronageSettings, CrownGrant


class DatabaseInvariantTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='model-owner')
        self.player = Player.objects.create(name='模型陪玩', player_type=PlayerType.objects.create(name='模型类型', priority=1))
        self.purchase = PatronagePurchase.objects.create(boss=self.user, player=self.player, player_name=self.player.name,
            package_code='day', package_name='日冠', amount_yuan=Decimal('188'), commission_rate=Decimal('0.25'),
            platform_amount_yuan=Decimal('47'), player_amount_yuan=Decimal('141'),
            price_version='d'*64, config_snapshot={'contract_version':'1.0'}, idempotency_key='model-key')

    def test_database_snapshot_cannot_be_rewritten_by_queryset_update(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            PatronagePurchase.objects.filter(pk=self.purchase.pk).update(
                amount_yuan=Decimal('200'), platform_amount_yuan=Decimal('50'), player_amount_yuan=Decimal('150'))
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.amount_yuan, Decimal('188'))

    def test_purchase_interval_cannot_have_missing_end(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            PatronagePurchase.objects.filter(pk=self.purchase.pk).update(starts_at=timezone.now(), expires_at=None)

    def test_business_switch_defaults_off_and_paid_timestamp_is_database_guarded(self):
        config = PatronageSettings.objects.create()
        self.assertFalse(config.purchase_enabled)
        PatronageSettings.objects.filter(pk=config.pk).update(purchase_enabled=True)
        from apps.patronage.pricing import catalog_data
        self.assertFalse(catalog_data(self.player)['purchase_enabled'])  # runtime gate remains off
        with self.assertRaises(DatabaseError), transaction.atomic():
            PatronagePurchase.objects.filter(pk=self.purchase.pk).update(payment_status='paid')

    def test_duplicate_idempotency_and_invalid_split_are_rejected(self):
        for changes in ({'idempotency_key':'model-key'}, {'idempotency_key':'other', 'platform_amount_yuan':Decimal('1')}):
            with self.subTest(changes=changes), self.assertRaises(DatabaseError), transaction.atomic():
                PatronagePurchase.objects.create(boss=self.user, player=self.player, player_name=self.player.name,
                    package_code='day', package_name='日冠', amount_yuan=Decimal('188'), commission_rate=Decimal('0.25'),
                    player_amount_yuan=Decimal('141'), price_version='e'*64, config_snapshot={'contract_version':'1.0'},
                    **{'platform_amount_yuan':Decimal('47'), **changes})
