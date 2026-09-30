from decimal import Decimal
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError, PermissionDenied
from django.test import TestCase
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.gifts.models import Gift


class CommissionTests(TestCase):
    def setUp(self):
        self.operator = get_user_model().objects.create_superuser('operator', password='test')
        self.player = Player.objects.create(name='p', player_type=PlayerType.objects.create(name='t', priority=1))
        self.gift = Gift.objects.create(name='g', price_diamonds=10)

    def test_explicit_rate_defaults_and_audit(self):
        self.assertTrue(any(m.__name__ == 'PlayerGiftConfig' for m in apps.get_app_config('gifts').get_models()))
        from apps.gifts.services.configuration import initialize_configs, update_config, resolve_commission
        from apps.gifts.models import PlayerGiftConfig, PlayerGiftConfigAudit
        self.assertEqual(initialize_configs(actor=self.operator, players=[self.player], gifts=[self.gift], reason='initial'), 1)
        config = PlayerGiftConfig.objects.get(player=self.player, gift=self.gift)
        self.assertEqual(config.commission_rate, Decimal('25.00'))
        update_config(actor=self.operator, config_id=config.pk, commission_rate=Decimal('0.00'), is_enabled=True, reason='free rate', expected_version=1)
        self.assertEqual(initialize_configs(actor=self.operator, players=[self.player], gifts=[self.gift], reason='repeat'), 0)
        snapshot = resolve_commission(self.player.pk, self.gift.pk, expected_version=2)
        self.assertEqual(snapshot['commission_rate'], '0.00')
        self.assertEqual(PlayerGiftConfigAudit.objects.count(), 2)
        self.assertEqual(PlayerGiftConfigAudit.objects.order_by('id').last().before['commission_rate'], '25.00')
        with self.assertRaises(ValidationError):
            resolve_commission(self.player.pk, self.gift.pk, expected_version=1)
        with self.assertRaises(ValidationError):
            resolve_commission(self.player.pk, 99999)

    def test_admin_configuration_and_read_only_audit(self):
        from apps.gifts.models import PlayerGiftConfig, PlayerGiftConfigAudit
        self.client.force_login(self.operator)
        response = self.client.post('/admin/gifts/playergiftconfig/add/', {
            'player': self.player.pk, 'gift': self.gift.pk, 'commission_rate': '25.00',
            'is_enabled': 'on', 'reason': 'initial', '_save': 'Save'})
        self.assertEqual(response.status_code, 302)
        config = PlayerGiftConfig.objects.get()
        response = self.client.post('/admin/gifts/playergiftconfig/%s/change/' % config.pk, {
            'commission_rate': '0.00', 'is_enabled': 'on', 'reason': 'update', 'expected_version': 1, '_save': 'Save'})
        self.assertEqual(response.status_code, 302)
        config.refresh_from_db()
        self.assertEqual(config.commission_rate, Decimal('0.00'))
        self.assertEqual(config.audits.count(), 2)
        self.assertEqual(self.client.post('/admin/gifts/playergiftconfigaudit/1/delete/').status_code, 403)

    def test_untracked_edits_invalid_rates_and_permissions_rejected(self):
        from apps.gifts.services.configuration import initialize_configs, update_config, resolve_commission
        from apps.gifts.models import PlayerGiftConfig
        initialize_configs(actor=self.operator, players=[self.player], gifts=[self.gift], reason='initial')
        config = PlayerGiftConfig.objects.get()
        with self.assertRaises(ValidationError):
            config.save()
        for rate in ('-0.01', '100.01', '1.001', 'NaN', 0.1, True):
            with self.subTest(rate=rate), self.assertRaises(ValidationError):
                update_config(actor=self.operator, config_id=config.pk, commission_rate=rate,
                              is_enabled=True, reason='bad', expected_version=1)
        user = get_user_model().objects.create_user('plain')
        with self.assertRaises(PermissionDenied):
            initialize_configs(actor=user, players=[self.player], gifts=[self.gift], reason='bad')
        update_config(actor=self.operator, config_id=config.pk, commission_rate='100.00', is_enabled=False, reason='disable', expected_version=1)
        with self.assertRaises(ValidationError):
            resolve_commission(self.player.pk, self.gift.pk)
