"""Real isolated-DB transactions; external payment/network boundaries stay offline."""
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError
from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.players.archive import archive_players
from apps.wallet.models import ClientWallet
from apps.gifts.models import Gift, PlayerGiftConfig, GiftTransfer, GiftPurchase
from apps.gifts.services import purchases, transfers


@override_settings(GIFT_PURCHASE_ENABLED=True, GIFT_INVENTORY_SEND_ENABLED=True,
    GIFT_TRANSACTION_POLICY={'version': 'archive-test', 'platform_approved': True,
        'max_quantity': 10, 'max_diamonds': 10000, 'daily_diamonds': None,
        'recipient_ids': None, 'recipient_scope': 'approved'})
class ArchiveTransactionTests(TransactionTestCase):
    def setUp(self):
        self.boss = get_user_model().objects.create_user('archive-boss')
        self.profile = ClientProfile.objects.create(user=self.boss, openid='archive-boss')
        self.wallet, _ = ClientWallet.objects.update_or_create(profile=self.profile, defaults={'balance': Decimal('5000')})
        self.receiver = get_user_model().objects.create_user('archive-recipient')
        ClientProfile.objects.create(user=self.receiver, openid='archive-recipient', nickname='接收者')
        self.player = Player.objects.create(user=self.receiver, name='原陪玩', player_type=PlayerType.objects.create(name='技术', priority=1))
        self.gift = Gift.objects.create(name='测试礼物', price_diamonds=10, send_enabled=True)
        Gift.objects.filter(pk=self.gift.pk).update(is_active=True)
        PlayerGiftConfig(player=self.player, gift=self.gift, commission_rate=Decimal('0')).save(_audited=True)
        self.intent = dict(gift_code=self.gift.code, quantity=1, mode='direct', recipient_id=self.player.pk)

    def archive(self):
        archive_players([self.player.pk], actor=self.boss, reason='结束合作')

    def test_archived_player_can_still_read_and_submit_withdrawal(self):
        from apps.earnings.models import PlayerWallet, EarningsConfig, Withdrawal
        from rest_framework.test import APIClient
        EarningsConfig.objects.update_or_create(pk=1, defaults={'min_withdrawal_amount': Decimal('1')})
        wallet, _ = PlayerWallet.objects.update_or_create(player=self.player, defaults={'available_balance': Decimal('100')})
        self.archive()
        client = APIClient()
        client.force_authenticate(self.receiver)
        self.assertEqual(client.get('/api/player/earnings/overview').status_code, 200)
        result = client.post('/api/player/earnings/withdrawals', {'amount': '10', 'payment_method': 'wechat', 'account_name': '测试', 'account_no': 'isolated-fixture'}, format='json')
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(Withdrawal.objects.get(player=self.player).amount, Decimal('10'))
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal('90'))

    def test_prior_confirmed_gift_delivery_survives_archive(self):
        quote = purchases.quote(self.boss, **self.intent)
        with patch('apps.gifts.services.purchases.fulfill', side_effect=RuntimeError('simulated crash after capture')):
            with self.assertRaises(RuntimeError):
                purchases.purchase(self.boss, **self.intent, price_version=quote['price_version'],
                    commission_version=quote['commission_version'], idempotency_key='before-archive')
        self.archive()
        result = purchases.purchase(self.boss, **self.intent, price_version=quote['price_version'],
            commission_version=quote['commission_version'], idempotency_key='before-archive')
        self.assertEqual(result.status, 'paid')
        self.assertEqual(GiftTransfer.objects.count(), 1)

    def test_new_direct_prepared_before_archive_cannot_first_dispatch(self):
        quote = purchases.quote(self.boss, **self.intent)
        with patch('apps.wallet.spend_service.execute', side_effect=RuntimeError('before dispatch')):
            with self.assertRaises(RuntimeError):
                purchases.purchase(self.boss, **self.intent, price_version=quote['price_version'],
                    commission_version=quote['commission_version'], idempotency_key='prepared')
        record = GiftPurchase.objects.get()
        self.assertEqual(record.attempt.status, 'prepared')
        self.archive()
        with self.assertRaises(ValidationError):
            purchases.purchase(self.boss, **self.intent, price_version=quote['price_version'],
                commission_version=quote['commission_version'], idempotency_key='prepared')
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('5000'))
        self.assertFalse(GiftTransfer.objects.exists())

    def test_new_direct_and_inventory_sends_fail_without_mutating_money(self):
        quote = purchases.quote(self.boss, **self.intent)
        self.archive()
        with self.assertRaises(ValidationError):
            purchases.purchase(self.boss, **self.intent, price_version=quote['price_version'],
                commission_version=quote['commission_version'], idempotency_key='after-archive')
        with self.assertRaises(ValidationError):
            transfers.transfer(self.boss, gift_code=self.gift.code, quantity=1, recipient_id=self.player.pk,
                commission_version=1, idempotency_key='after-archive-inventory')
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('5000'))
        self.assertFalse(GiftPurchase.objects.exists())
        self.assertFalse(GiftTransfer.objects.exists())

    @override_settings(PATRONAGE_PURCHASE_ENABLED=True)
    def test_paid_patronage_and_wallet_history_survive_archive(self):
        from apps.patronage.models import PatronageSettings, PlayerPatronageConfig, CrownGrant, PatronageEarning
        from apps.patronage.quotes import quote_data
        from apps.patronage.purchases import purchase
        from apps.patronage.serializers import RecordSerializer
        from apps.earnings.models import PlayerWallet, WalletLedger
        from apps.players.archive_admin import preview
        PatronageSettings.objects.create(enabled=True, purchase_enabled=True)
        PlayerPatronageConfig.objects.create(player=self.player, hourly_rate_yuan=Decimal('50'))
        q = quote_data(self.boss, self.player, 'day_pass')
        record = purchase(self.boss, player_id=self.player.pk, package_code='day_pass', price_version=q['price_version'], idempotency_key='paid-patronage')
        self.assertEqual(record.payment_status, 'paid')
        wallet = PlayerWallet.objects.get(player=self.player)
        before = (wallet.available_balance, list(WalletLedger.objects.values()), list(CrownGrant.objects.values()), list(PatronageEarning.objects.values()))
        self.assertTrue(preview([self.player])[0]['crowns'])
        self.archive()
        wallet.refresh_from_db()
        after = (wallet.available_balance, list(WalletLedger.objects.values()), list(CrownGrant.objects.values()), list(PatronageEarning.objects.values()))
        self.assertEqual(before, after)
        record.refresh_from_db()
        self.assertEqual(RecordSerializer(record).data['player_name'], '原陪玩（已离开）')
        self.assertEqual(RecordSerializer(record).data['payment_status'], 'paid')

    @override_settings(PATRONAGE_PURCHASE_ENABLED=True)
    def test_patronage_prepared_before_archive_cannot_first_dispatch(self):
        from apps.patronage.models import PatronageSettings, PlayerPatronageConfig
        from apps.patronage.quotes import quote_data
        from apps.patronage.purchases import prepare, authorize_dispatch
        from django.db import transaction
        PatronageSettings.objects.create(enabled=True, purchase_enabled=True)
        PlayerPatronageConfig.objects.create(player=self.player, hourly_rate_yuan=Decimal('50'))
        q = quote_data(self.boss, self.player, 'day_pass')
        record, _ = prepare(self.boss, player_id=self.player.pk, package_code='day_pass', price_version=q['price_version'], idempotency_key='before-archive')
        self.archive()
        with self.assertRaises(ValidationError), transaction.atomic():
            authorize_dispatch(record.attempt)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('5000'))
        self.assertFalse(record.crowns.exists())
