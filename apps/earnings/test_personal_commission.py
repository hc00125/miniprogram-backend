from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.contrib.auth import get_user_model
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.gifts.models import Gift, GiftPurchase, GiftInventoryLot, GiftTransfer
from apps.gifts.services.configuration import initialize_configs, update_config


class InternalSurchargeConfigTests(TestCase):
    def setUp(self):
        self.operator = get_user_model().objects.create_superuser('operator', password='test')
        self.player = Player.objects.create(name='p', player_type=PlayerType.objects.create(name='t', priority=1))

    def test_internal_item_requires_no_fake_price_and_is_never_a_gift(self):
        self.assertIn('kind', [f.name for f in Gift._meta.fields])
        internal = Gift.objects.create(name='订单加价', code='order_surcharge', kind='internal_surcharge')
        self.assertIsNone(internal.price_diamonds)
        self.assertFalse(internal.image)
        self.assertEqual(self.client.get('/api/gifts/catalog/').json()['results'], [])
        with self.assertRaises(ValidationError):
            GiftPurchase.objects.create(purchase_no='p1', buyer=self.operator, gift=internal, quantity=1,
                idempotency_key='pkey', request_digest='a'*64, snapshot={'name': 'internal'})
        with self.assertRaises(ValidationError):
            GiftInventoryLot.objects.create(owner=self.operator, gift=internal, source='admin_free',
                source_reference='x', quantity=1, remaining=1, snapshot={'name': 'internal'})
        with self.assertRaises(ValidationError):
            GiftTransfer.objects.create(transfer_no='t1', sender=self.operator, recipient=self.player, gift=internal,
                quantity=1, idempotency_key='tkey', request_digest='a'*64, commission_snapshot={'rate': '25'})
        with self.assertRaises(ValidationError):
            internal.delete()
        internal.kind = 'gift'
        internal.price_diamonds = 10
        with self.assertRaises(ValidationError):
            internal.save()

    def test_adapter_fixed_25_preserves_internal_history_and_ordinary_personal_rate(self):
        from apps.earnings.personal_commission import get_surcharge_commission
        from apps.gifts.services.configuration import resolve_commission
        internal = Gift.objects.create(name='订单加价', code='order_surcharge', kind='internal_surcharge')
        ordinary = Gift.objects.create(name='普通礼物', price_diamonds=10)
        initialize_configs(actor=self.operator, players=[self.player], gifts=[ordinary], reason='ordinary')
        self.assertEqual(get_surcharge_commission(self.player)['commission_rate'], '25.00')
        initialize_configs(actor=self.operator, players=[self.player], gifts=[internal], reason='internal')
        config = self.player.playergiftconfig_set.get(gift=internal)
        update_config(actor=self.operator, config_id=config.pk, commission_rate=Decimal('0.00'),
                      is_enabled=True, reason='historical zero', expected_version=1)
        self.assertEqual(get_surcharge_commission(self.player)['commission_rate'], '25.00')
        config.refresh_from_db(); self.assertEqual(config.commission_rate, Decimal('0.00'))
        internal.internal_enabled = False
        internal.save()
        self.assertEqual(get_surcharge_commission(self.player)['commission_rate'], '25.00')
        ordinary_config = self.player.playergiftconfig_set.get(gift=ordinary)
        update_config(actor=self.operator, config_id=ordinary_config.pk, commission_rate=Decimal('7.00'),
                      is_enabled=True, reason='ordinary personal', expected_version=1)
        self.assertEqual(resolve_commission(self.player.pk, ordinary.pk)['commission_rate'], '7.00')
        self.assertEqual(get_surcharge_commission(self.player)['commission_rate'], '25.00')

