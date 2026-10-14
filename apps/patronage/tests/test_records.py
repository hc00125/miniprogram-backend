from decimal import Decimal
from django.apps import apps
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.patronage.models import PatronageSettings


class RecordTests(TestCase):
    def test_private_records_preserve_payment_state_and_frozen_prices(self):
        response = APIClient().get('/api/patronage/records/')
        self.assertIn(response.status_code, (401, 403))
        Purchase = apps.get_model('patronage', 'PatronagePurchase')
        owner = get_user_model().objects.create_user(username='record-owner')
        other = get_user_model().objects.create_user(username='record-other')
        player = Player.objects.create(name='记录陪玩', player_type=PlayerType.objects.create(name='记录类型', priority=1))
        purchase = Purchase.objects.create(boss=owner, player=player, player_name=player.name,
            package_code='day', package_name='日冠', amount_yuan=Decimal('188.00'),
            commission_rate=Decimal('0.25'), platform_amount_yuan=Decimal('47.00'), player_amount_yuan=Decimal('141.00'),
            price_version='b'*64, config_snapshot={'contract_version': '1.0'}, idempotency_key='record-1')
        client = APIClient()
        client.force_authenticate(other)
        self.assertEqual(client.get('/api/patronage/records/').json()['count'], 0)
        client.force_authenticate(owner)
        PatronageSettings.objects.create(day_price_yuan=Decimal('200.00'), commission_rate=Decimal('0'))
        data = client.get('/api/patronage/records/').json()
        self.assertEqual(set(data), {'count', 'next', 'previous', 'results'})
        row = data['results'][0]
        self.assertEqual(row['amount_yuan'], '188.00')
        self.assertEqual(row['amount_diamonds'], '1880.0')
        self.assertEqual(row['player_id'], player.pk)
        self.assertEqual(row['payment_status'], 'created')
        self.assertIsNone(row['paid_at'])
        self.assertIsNone(row['starts_at'])
        self.assertIsNone(row['expires_at'])
        self.assertTrue(row['created_at'].endswith('Z') or '+' in row['created_at'])
        self.assertEqual(row['blockers'], [])
        self.assertEqual(row['idempotency_key'], 'record-1')
        self.assertEqual(row['price_version'], 'b'*64)
        for state in ('processing', 'unknown', 'failed'):
            purchase.payment_status = state
            purchase.save(update_fields=['payment_status'])
            self.assertEqual(client.get('/api/patronage/records/').json()['results'][0]['payment_status'], state)
        purchase.amount_yuan = Decimal('200')
        with self.assertRaises(ValidationError):
            purchase.save()
        model_admin = admin.site._registry[Purchase]
        self.assertFalse(model_admin.has_add_permission(None))
        self.assertFalse(model_admin.has_delete_permission(None, purchase))
