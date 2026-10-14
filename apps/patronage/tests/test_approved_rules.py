"""Contract v3 rules explicitly approved after the v1/v2 baseline."""
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.patronage.models import PlayerPatronageConfig
from rest_framework.test import APIClient


class ApprovedConfigurationTests(TestCase):
    def test_admin_rejects_non_half_yuan_step_with_clear_message(self):
        root = get_user_model().objects.create_superuser('v3-admin', 'admin@example.test')
        self.client.force_login(root)
        player = Player.objects.create(name='half-yuan',
            player_type=PlayerType.objects.create(name='half-yuan-type', priority=1))
        url = '/admin/patronage/playerpatronageconfig/add/'
        response = self.client.post(url, {'player': player.pk, 'hourly_rate_yuan': '50.01',
            'enabled': 'on', '_save': 'Save'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '0.5')
        self.assertFalse(PlayerPatronageConfig.objects.exists())
        response = self.client.post(url, {'player': player.pk, 'hourly_rate_yuan': '50.50',
            'enabled': 'on', '_save': 'Save'})
        self.assertEqual(response.status_code, 302)
        config = PlayerPatronageConfig.objects.get()
        self.assertEqual(config.hourly_rate_yuan, Decimal('50.50'))
        package = APIClient().get('/api/patronage/catalog/', {'player_id': player.pk}).json()['packages'][-1]
        self.assertEqual(Decimal(package['amount_yuan']), Decimal('50.5')*7)

    def test_half_yuan_step_is_enforced_in_database_and_stale_catalog(self):
        from django.db import IntegrityError, transaction
        from apps.patronage.models import PatronageSettings
        from apps.patronage.pricing import catalog_data
        player = Player.objects.create(name='step-db',
            player_type=PlayerType.objects.create(name='step-db-type', priority=1))
        config = PlayerPatronageConfig.objects.create(player=player, hourly_rate_yuan=Decimal('50'))
        with self.assertRaises(IntegrityError), transaction.atomic():
            PlayerPatronageConfig.objects.filter(pk=config.pk).update(hourly_rate_yuan=Decimal('50.01'))
        config.hourly_rate_yuan = Decimal('50.01')
        package = catalog_data(player, config_pair=(PatronageSettings(), config))['packages'][-1]
        self.assertFalse(package['available'])
        self.assertIn('HOURLY_RATE_STEP_INVALID', package['blockers'])
        self.assertIsNone(package['amount_yuan'])

    def test_annual_duration_is_365_and_no_longer_unconfirmed(self):
        player = Player.objects.create(name='year-player',
            player_type=PlayerType.objects.create(name='year-type', priority=1))
        client = APIClient()
        user = get_user_model().objects.create_user('year-buyer')
        client.force_authenticate(user)
        catalog = client.get('/api/patronage/catalog/').json()
        self.assertEqual(next(p for p in catalog['packages'] if p['code']=='year')['duration_days'], 365)
        quote = client.post('/api/patronage/quotes/', {'player_id': player.pk, 'package_code': 'year'}, format='json').json()
        self.assertNotIn('YEAR_DURATION_UNCONFIRMED', quote['blockers'])
        self.assertIn('PURCHASE_NOT_ENABLED', quote['blockers'])
        self.assertFalse(quote['can_submit'])
