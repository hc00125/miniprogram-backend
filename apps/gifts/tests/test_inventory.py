from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from apps.gifts.models import Gift


class InventoryFoundationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('owner')
        self.gift = Gift.objects.create(name='g', price_diamonds=10)

    def test_lot_bounds_free_zero_earning_and_immutable_ledger(self):
        self.assertTrue(any(m.__name__ == 'GiftInventoryLot' for m in apps.get_app_config('gifts').get_models()))
        Lot = apps.get_model('gifts', 'GiftInventoryLot')
        Ledger = apps.get_model('gifts', 'GiftInventoryLedger')
        lot = Lot.objects.create(owner=self.user, gift=self.gift, source='admin_free', source_reference='grant1',
                                 quantity=3, remaining=3, snapshot={'name': 'g'})
        self.assertFalse(lot.earnings_eligible)
        self.assertEqual(lot.cost_diamonds, 0)
        for kwargs in ({'remaining': 4}, {'remaining': -1}, {'reserved': 4}, {'earnings_eligible': True}, {'cost_diamonds': 1}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Lot.objects.filter(pk=lot.pk).update(**kwargs)
        ledger = Ledger.objects.create(lot=lot, event_key='grant1', kind='grant', quantity_delta=3, actor=self.user, reason='test')
        ledger.reason = 'rewritten'
        with self.assertRaises(ValidationError):
            ledger.save()
        with self.assertRaises(ValidationError):
            ledger.delete()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Ledger.objects.bulk_create([Ledger(lot=lot, event_key='grant1', kind='grant', quantity_delta=3, actor=self.user, reason='repeat')])
