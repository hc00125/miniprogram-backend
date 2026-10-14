from decimal import Decimal
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.gifts.models import Gift, GiftPurchase, GiftTransfer, GiftInventoryLot, GiftTransferAllocation


class SettlementFoundationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('sender')
        self.player = Player.objects.create(name='p', player_type=PlayerType.objects.create(name='t', priority=1))
        self.gift = Gift.objects.create(name='g', price_diamonds=10)
        self.transfer = GiftTransfer.objects.create(transfer_no='t1', sender=self.user, recipient=self.player,
            gift=self.gift, quantity=1, idempotency_key='key', request_digest='a' * 64,
            commission_snapshot={'commission_rate': '25.00', 'version': 1})
        self.lot = GiftInventoryLot.objects.create(owner=self.user, gift=self.gift, source='admin_free',
            source_reference='g1', quantity=1, remaining=1, snapshot={'name': 'g'})
        self.allocation = GiftTransferAllocation.objects.create(transfer=self.transfer, lot=self.lot,
            quantity=1, snapshot={'earnings_eligible': False})

    def test_earning_rejects_undelivered_and_free_money_and_checks_conservation(self):
        self.assertTrue(any(m.__name__ == 'GiftEarning' for m in apps.get_app_config('gifts').get_models()))
        Earning = apps.get_model('gifts', 'GiftEarning')
        data = dict(transfer=self.transfer, allocation=self.allocation, player=self.player,
                    snapshot={'commission_rate': '25.00'}, earnings_eligible=False)
        with self.assertRaises(ValidationError):
            Earning.objects.create(**data)
        self.transfer.status = 'delivered'
        self.transfer.save()
        earning = Earning.objects.create(**data)
        self.assertEqual(earning.net_amount, Decimal('0.00'))
        self.assertEqual(earning.status, 'frozen')
        for kwargs in ({'gross_amount': 1}, {'available_amount': 1}, {'net_amount': -1},
                       {'gross_amount': 1, 'net_amount': 1}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Earning.objects.filter(pk=earning.pk).update(**kwargs)

    def test_refund_has_independent_steps_and_positive_quantity(self):
        self.assertTrue(any(m.__name__ == 'GiftRefund' for m in apps.get_app_config('gifts').get_models()))
        Refund = apps.get_model('gifts', 'GiftRefund')
        purchase = GiftPurchase.objects.create(purchase_no='p1', buyer=self.user, gift=self.gift,
            quantity=2, idempotency_key='pkey', request_digest='a'*64, snapshot={'unit_price_diamonds': 10})
        refund = Refund.objects.create(refund_no='r1', purchase=purchase, quantity=1, diamonds=10,
            reason='manual review', requested_by=self.user)
        for field in ('wallet_status', 'coin_status', 'inventory_status', 'earning_status'):
            self.assertEqual(getattr(refund, field), 'blocked')
        self.assertEqual(refund.status, 'draft')
        for kwargs in ({'quantity': 0}, {'diamonds': -1}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Refund.objects.filter(pk=refund.pk).update(**kwargs)
