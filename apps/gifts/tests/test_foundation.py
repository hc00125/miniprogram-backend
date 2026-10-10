from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from apps.gifts.models import Gift


class PurchaseFoundationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('buyer')
        self.gift = Gift.objects.create(name='g', price_diamonds=10)

    def test_purchase_constraints_and_inventory_has_no_recipient_rate(self):
        self.assertTrue(any(m.__name__ == 'GiftPurchase' for m in apps.get_app_config('gifts').get_models()))
        Purchase = apps.get_model('gifts', 'GiftPurchase')
        data = dict(purchase_no='purchase1', buyer=self.user, gift=self.gift, quantity=2,
                    idempotency_key='key', request_digest='a' * 64,
                    snapshot={'unit_price_diamonds': 10, 'total_diamonds': 20})
        purchase = Purchase.objects.create(**data)
        self.assertEqual(purchase.status, 'created')
        self.assertIsNone(purchase.recipient_id)
        self.assertNotIn('commission_rate', purchase.snapshot)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Purchase.objects.filter(pk=purchase.pk).update(quantity=0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Purchase.objects.filter(pk=purchase.pk).update(mode='direct')
        purchase.snapshot['commission_rate'] = '25.00'
        with self.assertRaises(ValidationError):
            purchase.save()

    def test_financial_switches_default_closed(self):
        for flag in ('GIFT_PURCHASE_ENABLED', 'GIFT_INVENTORY_SEND_ENABLED', 'GIFT_EARNINGS_RELEASE_ENABLED', 'GIFT_REFUND_ENABLED', 'GIFT_ADMIN_FREE_GRANT_ENABLED'):
            self.assertIs(getattr(settings, flag, None), False, flag)
