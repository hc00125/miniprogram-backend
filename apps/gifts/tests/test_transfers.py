from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.gifts.models import Gift, GiftInventoryLot


class TransferFoundationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('sender')
        self.player = Player.objects.create(name='p', player_type=PlayerType.objects.create(name='t', priority=1))
        self.gift = Gift.objects.create(name='g', price_diamonds=10)

    def test_transfer_allocation_source_and_snapshot_constraints(self):
        self.assertTrue(any(m.__name__ == 'GiftTransfer' for m in apps.get_app_config('gifts').get_models()))
        Transfer = apps.get_model('gifts', 'GiftTransfer')
        Allocation = apps.get_model('gifts', 'GiftTransferAllocation')
        transfer = Transfer.objects.create(transfer_no='t1', sender=self.user, recipient=self.player, gift=self.gift,
                                           quantity=1, idempotency_key='key', request_digest='a' * 64,
                                           commission_snapshot={'commission_rate': '0.00', 'version': 1})
        self.assertEqual(transfer.status, 'created')
        lot = GiftInventoryLot.objects.create(owner=self.user, gift=self.gift, source='admin_free', source_reference='g1',
                                              quantity=2, remaining=2, snapshot={'name': 'g'})
        allocation = Allocation.objects.create(transfer=transfer, lot=lot, quantity=1, snapshot={'earnings_eligible': False})
        for kwargs in ({'quantity': 0}, {'source': 'direct'}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Transfer.objects.filter(pk=transfer.pk).update(**kwargs)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Allocation.objects.bulk_create([Allocation(transfer=transfer, lot=lot, quantity=1, snapshot={})])
        allocation.quantity = 3
        with self.assertRaises(ValidationError):
            allocation.save()
        self.assertFalse(self.gift.send_enabled)
