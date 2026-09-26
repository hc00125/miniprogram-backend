"""Gift-only acceptance: diamonds out, fish into the existing wallet.
All data are synthetic, and test settings deny real payment/network calls.
"""
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient
from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.wallet.models import ClientWallet
from apps.gifts.models import Gift, PlayerGiftConfig, GiftPurchase, GiftTransfer, GiftInventoryLot, GiftEarning
from apps.gifts.services import purchases, transfers
from apps.earnings.models import PlayerWallet, WalletLedger, EarningsConfig, Withdrawal
from apps.earnings.withdrawals import create_withdrawal, return_withdrawal, approve_withdrawal, mark_withdrawal_paid


@override_settings(GIFT_PURCHASE_ENABLED=True, GIFT_INVENTORY_SEND_ENABLED=True,
                   WECHAT_VIRTUALPAY_ENV=1, FISH_CRACKER_EXCHANGE_RATE=10)
class ImmediateGiftIncomeTests(TransactionTestCase):
    def setUp(self):
        self.buyer = User.objects.create_user('gift-income-buyer')
        self.profile = ClientProfile.objects.create(user=self.buyer, openid='offline-gift-income-buyer')
        self.wallet, _ = ClientWallet.objects.update_or_create(profile=self.profile, defaults={'balance': Decimal('100.00')})
        receiver = User.objects.create_user('gift-income-receiver')
        ClientProfile.objects.create(user=receiver, openid='offline-gift-income-receiver', nickname='offline-gift-income-receiver')
        self.player = Player.objects.create(user=receiver, name='Synthetic recipient',
            player_type=PlayerType.objects.create(name='gift-income-type', priority=1))
        self.gift = Gift.objects.create(name='Synthetic gift', price_diamonds=10, send_enabled=True)
        Gift.objects.filter(pk=self.gift.pk).update(is_active=True)
        self.gift.refresh_from_db()
        self.config = PlayerGiftConfig(player=self.player, gift=self.gift, commission_rate=Decimal('25.00'))
        self.config.save(_audited=True)
        self.policy = {'version': 'offline-gift-immediate', 'platform_approved': True,
            'max_quantity': 100, 'max_diamonds': 1000, 'daily_diamonds': 2000,
            'recipient_ids': [self.player.pk]}
        self.override = override_settings(GIFT_TRANSACTION_POLICY=self.policy)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.client = APIClient()
        self.client.force_authenticate(self.buyer)
        EarningsConfig.objects.update_or_create(key='default', defaults={'min_withdrawal_amount': Decimal('0.10')})

    def buy(self, *, mode='direct', quantity=2, key='direct'):
        choice = {'gift_code': self.gift.code, 'quantity': quantity, 'mode': mode}
        if mode == 'direct':
            choice['recipient_id'] = self.player.pk
        q = purchases.quote(self.buyer, **choice)
        return purchases.purchase(self.buyer, **choice, price_version=q['price_version'],
            commission_version=q['commission_version'], idempotency_key=key)

    def test_diamond_purchase_credits_fish_immediately_and_exactly_once(self):
        purchase = self.buy()
        earning = GiftEarning.objects.get()
        self.assertEqual(earning.status, 'available')
        self.assertTrue(earning.snapshot['monetary_accrual'])
        self.assertEqual(earning.gross_amount, Decimal('20.00'))
        self.assertEqual(earning.commission_amount, Decimal('5.00'))
        self.assertEqual(earning.net_amount, Decimal('15.00'))
        player_wallet = PlayerWallet.objects.get(player=self.player)
        self.assertEqual(player_wallet.available_balance, Decimal('15.00'))
        self.assertEqual(player_wallet.pending_balance, Decimal('0.00'))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('98.00'))
        self.assertEqual(self.buy().pk, purchase.pk)
        self.assertEqual(GiftEarning.objects.count(), 1)
        self.assertEqual(WalletLedger.objects.filter(reference_type='gift_earning', reference_id=str(earning.pk)).count(), 1)
        self.assertFalse(GiftInventoryLot.objects.filter(owner=self.player.user).exists())
        player_wallet.refresh_from_db()
        self.assertEqual(player_wallet.available_balance, Decimal('15.00'))

    def test_inventory_quote_and_send_use_owned_stock_without_a_second_charge(self):
        self.buy(mode='inventory', quantity=3, key='stock')
        self.assertFalse(GiftEarning.objects.exists())
        before = ClientWallet.objects.get(pk=self.wallet.pk).balance
        choice = {'gift_code': self.gift.code, 'quantity': 2, 'recipient_id': self.player.pk}
        q = self.client.post('/api/gifts/inventory/quotes/', choice, format='json')
        self.assertEqual(q.status_code, 200)
        self.assertEqual(q.data['available_quantity'], 3)
        self.assertEqual(q.data['payment_diamonds'], 0)
        self.assertTrue(q.data['can_submit'])
        response = self.client.post('/api/gifts/transfers/', dict(choice,
            commission_version=q.data['commission_version'], idempotency_key='stock-send'), format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'delivered')
        self.assertEqual(response.data['gift_code'], self.gift.code)
        self.assertEqual(ClientWallet.objects.get(pk=self.wallet.pk).balance, before)
        self.assertEqual(GiftInventoryLot.objects.get().remaining, 1)
        self.assertEqual(PlayerWallet.objects.get(player=self.player).available_balance, Decimal('15.00'))
        readback = self.client.get('/api/gifts/transfers/by-key/', {'idempotency_key': 'stock-send'})
        self.assertEqual(readback.data['transfer_no'], response.data['transfer_no'])
        duplicate = self.client.post('/api/gifts/transfers/', dict(choice,
            commission_version=q.data['commission_version'], idempotency_key='stock-send'), format='json')
        self.assertEqual(duplicate.data['transfer_no'], response.data['transfer_no'])
        self.assertEqual(GiftEarning.objects.count(), 1)

    def test_existing_withdrawal_accepts_gift_income_and_return_then_payout(self):
        self.buy()
        withdrawal = create_withdrawal(self.player, Decimal('15.00'), Withdrawal.METHOD_WECHAT,
            'Synthetic name', 'synthetic-account')
        returned = return_withdrawal(withdrawal, 'synthetic rejection')
        self.assertEqual(returned.status, Withdrawal.STATUS_REJECTED)
        self.assertEqual(PlayerWallet.objects.get(player=self.player).available_balance, Decimal('15.00'))
        withdrawal = create_withdrawal(self.player, Decimal('15.00'), Withdrawal.METHOD_WECHAT,
            'Synthetic name', 'synthetic-account')
        withdrawal = approve_withdrawal(withdrawal)
        withdrawal.transfer_no = 'synthetic-payout-proof'
        withdrawal.save(update_fields=['transfer_no'])
        mark_withdrawal_paid(withdrawal)
        wallet = PlayerWallet.objects.get(player=self.player)
        self.assertEqual(wallet.available_balance, Decimal('0.00'))
        self.assertEqual(wallet.withdrawing_balance, Decimal('0.00'))
        self.assertEqual(wallet.withdrawn_total, Decimal('15.00'))

    def test_approved_recipients_work_without_an_extra_allowlist_or_daily_cap(self):
        policy = dict(self.policy, recipient_scope='approved', daily_diamonds=None)
        policy.pop('recipient_ids')
        with override_settings(GIFT_TRANSACTION_POLICY=policy):
            self.assertEqual(self.buy(key='no-extra-limits').status, 'paid')

    def test_owned_stock_keeps_purchase_cost_after_catalog_price_changes(self):
        self.buy(mode='inventory', key='old-price')
        Gift.objects.filter(pk=self.gift.pk).update(price_diamonds=99, is_active=False)
        transfers.transfer(self.buyer, gift_code=self.gift.code, quantity=2,
            recipient_id=self.player.pk, commission_version=1, idempotency_key='old-price-send')
        self.assertEqual(GiftEarning.objects.get().net_amount, Decimal('15.00'))

    def test_net_income_uses_existing_debt_offset_rule_and_one_credit(self):
        PlayerWallet.objects.create(player=self.player, debt_balance=Decimal('4.00'))
        self.buy(key='debt-offset')
        wallet = PlayerWallet.objects.get(player=self.player)
        self.assertEqual(wallet.available_balance, Decimal('11.00'))
        self.assertEqual(wallet.debt_balance, Decimal('0.00'))
        self.assertEqual(GiftEarning.objects.get().debt_offset_amount, Decimal('4.00'))
        self.assertEqual(WalletLedger.objects.filter(reference_type='gift_earning').count(), 2)

    def test_stock_split_does_not_multiply_fractional_income(self):
        Gift.objects.filter(pk=self.gift.pk).update(price_diamonds=1)
        self.gift.refresh_from_db()
        PlayerGiftConfig.objects.filter(pk=self.config.pk).update(commission_rate=Decimal('33.33'))
        for i in range(3):
            self.buy(mode='inventory', quantity=1, key=f'split-{i}')
        transfers.transfer(self.buyer, gift_code=self.gift.code, quantity=3,
            recipient_id=self.player.pk, commission_version=1, idempotency_key='split-send')
        self.assertEqual(sum(GiftEarning.objects.values_list('gross_amount', flat=True)), Decimal('3.00'))
        self.assertEqual(sum(GiftEarning.objects.values_list('commission_amount', flat=True)), Decimal('1.00'))
        self.assertEqual(PlayerWallet.objects.get(player=self.player).available_balance, Decimal('2.00'))

    def test_stock_quote_cannot_count_other_owners_or_reserved_lots(self):
        self.buy(mode='inventory', key='reserved-stock')
        GiftInventoryLot.objects.update(reserved=1)
        choice = dict(gift_code=self.gift.code, quantity=2, recipient_id=self.player.pk)
        q = transfers.quote(self.buyer, **choice)
        self.assertEqual(q['available_quantity'], 1)
        self.assertFalse(q['can_submit'])
        self.assertFalse(GiftEarning.objects.exists())
        other = User.objects.create_user('other-stock-owner')
        ClientProfile.objects.create(user=other, openid='other-stock-owner', nickname='other-stock-owner')
        self.assertEqual(transfers.quote(other, **choice)['available_quantity'], 0)

    def test_failed_wallet_income_rolls_back_transfer_and_stock(self):
        self.buy(mode='inventory', key='failure-stock')
        with patch('apps.earnings.wallet.write_ledger', side_effect=RuntimeError('synthetic ledger failure')):
            with self.assertRaises(RuntimeError):
                transfers.transfer(self.buyer, gift_code=self.gift.code, quantity=1,
                    recipient_id=self.player.pk, commission_version=1, idempotency_key='failure-send')
        self.assertEqual(GiftInventoryLot.objects.get().remaining, 2)
        self.assertFalse(GiftTransfer.objects.exists())
        self.assertFalse(GiftEarning.objects.exists())
        self.assertFalse(PlayerWallet.objects.filter(player=self.player, available_balance__gt=0).exists())
