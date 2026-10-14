"""Zero-price test gifts are real deliveries, never fabricated wallet credits."""
from decimal import Decimal
from unittest.mock import patch
from django.test import TransactionTestCase, override_settings
from django.core.exceptions import ValidationError
from rest_framework.exceptions import ValidationError as APIValidationError
from apps.gifts.models import Gift, GiftPurchase, GiftTransfer, GiftInventoryLot, GiftEarning
from apps.gifts.services import purchases, transfers
from apps.wallet.models import ClientWallet, ClientWalletLedger, WalletSpendAttempt
from apps.earnings.models import PlayerWallet, WalletLedger
from . import test_immediate_income as fixtures


@override_settings(GIFT_PURCHASE_ENABLED=True, GIFT_INVENTORY_SEND_ENABLED=True,
                   WECHAT_VIRTUALPAY_ENV=1, FISH_CRACKER_EXCHANGE_RATE=10)
class FreeCatalogGiftTests(TransactionTestCase):
    setUp = fixtures.ImmediateGiftIncomeTests.setUp
    buy = fixtures.ImmediateGiftIncomeTests.buy

    def free_gift(self):
        self.gift.price_diamonds = 0
        self.gift.is_active = False
        self.gift.save()
        Gift.objects.filter(pk=self.gift.pk).update(is_active=True)
        self.gift.refresh_from_db()

    def test_free_direct_delivery_has_no_debit_no_earnings_and_is_idempotent(self):
        self.free_gift()
        ClientWallet.objects.filter(pk=self.wallet.pk).update(balance=0)
        before = ClientWalletLedger.objects.count()
        with patch('apps.wallet.spend_service.reserve', side_effect=AssertionError('no free reservation')), \
             patch('apps.wallet.spend_service.execute', side_effect=AssertionError('no free charge')):
            record = self.buy(key='free-direct')
            repeated = self.buy(key='free-direct')
        self.assertEqual(record.pk, repeated.pk)
        self.assertEqual(record.status, 'paid')
        self.assertIsNone(record.attempt_id)
        self.assertEqual(record.snapshot['total_diamonds'], 0)
        self.assertEqual(record.snapshot['payment_kind'], 'free')
        self.assertEqual(GiftTransfer.objects.count(), 1)
        self.assertEqual(ClientWallet.objects.get(pk=self.wallet.pk).balance, 0)
        self.assertEqual(ClientWalletLedger.objects.count(), before)
        self.assertFalse(WalletSpendAttempt.objects.exists())
        self.assertFalse(WalletLedger.objects.exists())
        earning = GiftEarning.objects.get()
        self.assertFalse(earning.earnings_eligible)
        self.assertEqual(earning.net_amount, 0)

    def test_free_stock_cannot_gain_income_after_a_later_catalog_price_increase(self):
        self.free_gift()
        self.buy(mode='inventory', quantity=2, key='free-stock')
        lot = GiftInventoryLot.objects.get()
        self.assertEqual(lot.cost_diamonds, 0)
        self.assertFalse(lot.earnings_eligible)
        self.assertFalse(GiftEarning.objects.exists())
        Gift.objects.filter(pk=self.gift.pk).update(price_diamonds=100)
        transfers.transfer(self.buyer, gift_code=self.gift.code, quantity=2,
            recipient_id=self.player.pk, commission_version=1, idempotency_key='send-free-stock')
        self.assertEqual(GiftInventoryLot.objects.get().remaining, 0)
        self.assertEqual(GiftEarning.objects.get().net_amount, 0)
        self.assertFalse(WalletLedger.objects.exists())
        self.assertFalse(WalletSpendAttempt.objects.exists())

    def test_one_diamond_still_uses_real_spend_and_paid_stock_keeps_its_income(self):
        Gift.objects.filter(pk=self.gift.pk).update(price_diamonds=1)
        record = self.buy(mode='inventory', quantity=1, key='one-diamond')
        self.assertIsNotNone(record.attempt_id)
        self.assertEqual(record.snapshot['total_diamonds'], 1)
        self.assertEqual(ClientWallet.objects.get(pk=self.wallet.pk).balance, Decimal('100') - Decimal('1') / 10)
        Gift.objects.filter(pk=self.gift.pk).update(price_diamonds=0)
        transfers.transfer(self.buyer, gift_code=self.gift.code, quantity=1,
            recipient_id=self.player.pk, commission_version=1, idempotency_key='send-paid-stock')
        self.assertEqual(PlayerWallet.objects.get(player=self.player).available_balance,
                         Decimal('1') * (1 - Decimal('25') / 100))

    def test_zero_to_paid_price_change_requires_new_confirmation_without_charging(self):
        self.free_gift()
        choice=dict(gift_code=self.gift.code, quantity=1, mode='direct', recipient_id=self.player.pk)
        q=purchases.quote(self.buyer, **choice)
        Gift.objects.filter(pk=self.gift.pk).update(price_diamonds=1)
        with self.assertRaises(APIValidationError) as caught:
            purchases.purchase(self.buyer, **choice, price_version=q['price_version'],
                commission_version=q['commission_version'], idempotency_key='changed-free-price')
        self.assertIn('PRICE_CHANGED', str(caught.exception))
        self.assertFalse(GiftPurchase.objects.exists())
        self.assertFalse(WalletSpendAttempt.objects.exists())

    def test_free_delivery_failure_rolls_back_all_records(self):
        self.free_gift()
        with patch('apps.gifts.services.transfers.credit_entitlement', side_effect=RuntimeError('offline failure')):
            with self.assertRaises(RuntimeError):self.buy(key='failed-free-delivery')
        self.assertFalse(GiftPurchase.objects.exists())
        self.assertFalse(GiftTransfer.objects.exists())
        self.assertFalse(WalletSpendAttempt.objects.exists())

    def test_free_api_receipt_and_recovery_are_zero_without_payment_login(self):
        self.free_gift()
        choice=dict(gift_code=self.gift.code, quantity=1, mode='direct', recipient_id=self.player.pk)
        q=self.client.post('/api/gifts/quotes/',choice,format='json')
        self.assertEqual(q.status_code,200)
        sent=self.client.post('/api/gifts/purchases/',dict(choice,price_version=q.data['price_version'],
            commission_version=q.data['commission_version'],idempotency_key='free-api',code=''),format='json')
        self.assertEqual(sent.status_code,200)
        self.assertEqual(sent.data['amount_diamonds'],0)
        back=self.client.get('/api/gifts/purchases/by-key/',{'idempotency_key':'free-api'})
        self.assertEqual(back.data,sent.data)

    def test_catalog_accepts_zero_but_not_negative_or_fractional_prices(self):
        self.free_gift()
        self.assertEqual(Gift.objects.get(pk=self.gift.pk).price_diamonds, 0)
        for invalid in (-1, Decimal('0.5'), True, '0.5'):
            with self.subTest(price=invalid), self.assertRaises(ValidationError):
                self.gift.price_diamonds = invalid
                self.gift.is_active = False
                self.gift.full_clean()
