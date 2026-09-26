from io import StringIO
from decimal import Decimal
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from apps.gifts.tests.test_configuration import CommissionTests


class ConfigurationBoundaryTests(TestCase):
    setUp = CommissionTests.setUp
    def test_batch_command_is_explicit_and_idempotent(self):
        from apps.gifts.models import PlayerGiftConfig
        out = StringIO()
        call_command('initialize_gift_configs', actor_id=self.operator.pk, player_ids=[self.player.pk],
                     gift_ids=[self.gift.pk], reason='test', stdout=out)
        self.assertEqual(PlayerGiftConfig.objects.count(), 1)
        call_command('initialize_gift_configs', actor_id=self.operator.pk, player_ids=[self.player.pk],
                     gift_ids=[self.gift.pk], reason='test', stdout=out)
        self.assertEqual(PlayerGiftConfig.objects.get().audits.count(), 1)

    def test_associations_are_independent_and_database_constraints_hold(self):
        from apps.gifts.models import Gift, PlayerGiftConfig
        from apps.gifts.services.configuration import initialize_configs, update_config, resolve_commission
        from apps.players.models import Player
        p2 = Player.objects.create(name='p2', player_type=self.player.player_type)
        g2 = Gift.objects.create(name='g2', price_diamonds=12)
        initialize_configs(actor=self.operator, players=[self.player, p2], gifts=[self.gift, g2], reason='test')
        config = PlayerGiftConfig.objects.get(player=self.player, gift=self.gift)
        update_config(actor=self.operator, config_id=config.pk, commission_rate=Decimal('10.00'), is_enabled=True, reason='edit', expected_version=1)
        self.assertEqual(resolve_commission(p2.pk, self.gift.pk)['commission_rate'], '25.00')
        self.assertEqual(resolve_commission(self.player.pk, g2.pk)['commission_rate'], '25.00')
        for kwargs in ({'commission_rate': -1}, {'commission_rate': 101}, {'version': 0}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                PlayerGiftConfig.objects.filter(pk=config.pk).update(**kwargs)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PlayerGiftConfig.objects.bulk_create([PlayerGiftConfig(player=self.player, gift=self.gift)])
