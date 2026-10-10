from decimal import Decimal
from django.apps import apps
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient
from apps.catalog.models import PlayerType
from apps.players.models import Player


class ConfigurationTests(TestCase):
    def test_admin_forms_persist_rmb_configuration_without_opening_purchase(self):
        from apps.patronage.models import PatronageSettings, PlayerPatronageConfig
        root = get_user_model().objects.create_superuser(username='patronage-admin', email='admin@example.test')
        self.client.force_login(root)
        data = {'enabled':'on', 'commission_rate':'0', 'day_price_yuan':'188.00',
                'week_price_yuan':'520.00', 'month_price_yuan':'1314.00',
                'quarter_price_yuan':'2888.00', 'year_price_yuan':'9999.00', '_save':'Save'}
        response = self.client.post('/admin/patronage/patronagesettings/add/', data)
        self.assertEqual(response.status_code, 302)
        config = PatronageSettings.objects.get()
        self.assertEqual(config.commission_rate, Decimal('0'))
        self.assertFalse(config.purchase_enabled)
        player = Player.objects.create(name='后台陪玩', player_type=PlayerType.objects.create(name='后台类型', priority=1))
        response = self.client.post('/admin/patronage/playerpatronageconfig/add/',
            {'player':player.pk, 'hourly_rate_yuan':'50.00', 'enabled':'on', '_save':'Save'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PlayerPatronageConfig.objects.get(player=player).hourly_rate_yuan, Decimal('50.00'))
        self.assertEqual(self.client.get('/admin/patronage/patronagepurchase/add/').status_code, 403)
        self.assertEqual(self.client.get('/admin/patronage/crowngrant/add/').status_code, 403)
        staff = get_user_model().objects.create_user(username='no-config-permission', is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get('/admin/patronage/patronagesettings/1/change/').status_code, 403)

    def test_persisted_global_and_individual_configuration_drive_catalog(self):
        models = {m.__name__: m for m in apps.get_app_config('patronage').get_models()}
        self.assertIn('PatronageSettings', models)
        self.assertIn('PlayerPatronageConfig', models)
        Global, Individual = models['PatronageSettings'], models['PlayerPatronageConfig']
        config = Global.objects.create(commission_rate=Decimal('0.00'), day_price_yuan=Decimal('200.00'))
        player = Player.objects.create(name='测试陪玩', player_type=PlayerType.objects.create(name='测试类型', priority=1))
        Individual.objects.create(player=player, hourly_rate_yuan=Decimal('50.00'))
        data = APIClient().get('/api/patronage/catalog/', {'player_id': player.pk}).json()
        self.assertEqual(data['player']['id'], player.pk)
        self.assertEqual(data['player']['hourly_rate_yuan'], '50.00')
        self.assertEqual(data['commission_rate'], '0.00')
        self.assertEqual(data['packages'][0]['amount_yuan'], '200.00')
        self.assertEqual(data['packages'][-1]['amount_yuan'], '350.00')
        self.assertEqual(data['packages'][-1]['amount_diamonds'], '3500.0')
        self.assertEqual(data['packages'][-1]['bonus_naming_days'], 7)
        self.assertFalse(config.purchase_enabled)
        config.commission_rate = Decimal('1.01')
        with self.assertRaises(ValidationError):
            config.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Global.objects.filter(pk=config.pk).update(commission_rate=Decimal('-0.01'))
        self.assertIn(Global, admin.site._registry)
        self.assertIn(Individual, admin.site._registry)
