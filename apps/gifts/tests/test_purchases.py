from decimal import Decimal
from importlib.util import find_spec
from django.contrib.auth.models import User
from django.test import TransactionTestCase, override_settings
from apps.accounts.models import ClientProfile
from apps.wallet.models import ClientWallet
from apps.gifts.models import Gift, GiftPurchase, GiftInventoryLot
from rest_framework.exceptions import ValidationError


@override_settings(WECHAT_VIRTUALPAY_ENV=1, GIFT_PURCHASE_ENABLED=True,
    GIFT_TRANSACTION_POLICY={'version': 'test-only-v1', 'platform_approved': True,
        'max_quantity': 10, 'max_diamonds': 1000, 'daily_diamonds': 2000, 'recipient_ids': []})
class PurchaseFlowTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('synthetic-gift-buyer')
        self.profile = ClientProfile.objects.create(user=self.user, openid='synthetic-gift')
        self.wallet, _ = ClientWallet.objects.update_or_create(profile=self.profile, defaults={'balance': Decimal('10.00')})
        self.gift = Gift.objects.create(name='Synthetic', price_diamonds=10, send_enabled=True)
        # Image validation belongs to catalog tests; bypass only this synthetic fixture.
        Gift.objects.filter(pk=self.gift.pk).update(is_active=True)
        self.gift.refresh_from_db()

    def service(self):
        self.assertIsNotNone(find_spec('apps.gifts.services.purchases'), 'Purchase service missing')
        from apps.gifts.services import purchases
        return purchases

    def test_direct_and_inventory_send_credit_zero_commission_income(self):
        from apps.players.models import Player
        from apps.catalog.models import PlayerType
        from apps.gifts.models import PlayerGiftConfig, GiftTransfer, GiftEarning
        receiver = User.objects.create_user('synthetic-receiver')
        ClientProfile.objects.create(user=receiver, openid='synthetic-receiver', nickname='synthetic-receiver')
        player = Player.objects.create(user=receiver, name='Synthetic receiver', player_type=PlayerType.objects.create(name='synthetic', priority=1))
        PlayerGiftConfig(player=player, gift=self.gift, commission_rate=Decimal('0')).save(_audited=True)
        p = {'version': 'test-only-v1', 'platform_approved': True, 'max_quantity': 10,
             'max_diamonds': 1000, 'daily_diamonds': 2000, 'recipient_ids': [player.pk]}
        service = self.service()
        with override_settings(GIFT_TRANSACTION_POLICY=p, GIFT_INVENTORY_SEND_ENABLED=True):
            intent = dict(gift_code=self.gift.code, quantity=2, mode='direct', recipient_id=player.pk)
            q = service.quote(self.user, **intent)
            purchase = service.purchase(self.user, **intent, price_version=q['price_version'],
                commission_version=q['commission_version'], idempotency_key='direct-key')
            self.assertEqual(purchase.status, 'paid')
            earning = GiftEarning.objects.get()
            self.assertEqual(earning.status, 'available')
            self.assertEqual(earning.snapshot['commission']['commission_rate'], '0.00')
            self.assertEqual(earning.net_amount, Decimal('20.00'))
            self.assertTrue(earning.snapshot['monetary_accrual'])
            self.assertEqual(earning.snapshot['blockers'], [])
            self.assertFalse(GiftInventoryLot.objects.filter(owner=receiver).exists())
            inventory = dict(gift_code=self.gift.code, quantity=3, mode='inventory')
            q = service.quote(self.user, **inventory)
            service.purchase(self.user, **inventory, price_version=q['price_version'], idempotency_key='inventory-key')
            self.assertIsNotNone(find_spec('apps.gifts.services.transfers'), 'Inventory send service missing')
            from apps.gifts.services.transfers import transfer
            sent = transfer(self.user, gift_code=self.gift.code, quantity=2, recipient_id=player.pk,
                commission_version=1, idempotency_key='send-key')
            self.assertEqual(transfer(self.user, gift_code=self.gift.code, quantity=2, recipient_id=player.pk,
                commission_version=1, idempotency_key='send-key').pk, sent.pk)
            self.wallet.refresh_from_db()
            self.assertEqual(self.wallet.balance, Decimal('5.00'))
            self.assertEqual(GiftInventoryLot.objects.get(owner=self.user).remaining, 1)
            self.assertEqual(GiftTransfer.objects.filter(status='delivered').count(), 2)
            self.assertEqual(GiftEarning.objects.count(), 2)

    def test_direct_recovery_uses_saved_policy_and_rate_not_new_sale_gates(self):
        from django.core.management import call_command
        from unittest.mock import patch
        from apps.players.models import Player
        from apps.catalog.models import PlayerType
        from apps.gifts.models import PlayerGiftConfig, GiftEarning
        receiver = User.objects.create_user('recover-receiver')
        ClientProfile.objects.create(user=receiver, openid='recover-receiver', nickname='recover-receiver')
        player = Player.objects.create(user=receiver, name='recover-receiver', player_type=PlayerType.objects.create(name='recover-type', priority=1))
        PlayerGiftConfig(player=player, gift=self.gift, commission_rate=Decimal('0')).save(_audited=True)
        p = {'version': 'test-v1', 'platform_approved': True, 'max_quantity': 10,
             'max_diamonds': 1000, 'daily_diamonds': 2000, 'recipient_ids': [player.pk]}
        service = self.service()
        intent = dict(gift_code=self.gift.code, quantity=1, mode='direct', recipient_id=player.pk)
        with override_settings(GIFT_TRANSACTION_POLICY=p):
            q = service.quote(self.user, **intent)
            with patch('apps.gifts.services.purchases.fulfill', side_effect=RuntimeError('crash')):
                with self.assertRaises(RuntimeError):
                    service.purchase(self.user, **intent, price_version=q['price_version'], commission_version=1, idempotency_key='direct-crash')
        with override_settings(GIFT_PURCHASE_ENABLED=False, GIFT_TRANSACTION_POLICY={}):
            call_command('reconcile_wallet_spends', apply=True)
        self.assertEqual(GiftPurchase.objects.get().status, 'paid')
        self.assertEqual(GiftEarning.objects.get().snapshot['commission']['commission_rate'], '0.00')

    def test_recovery_command_completes_delivery_once_when_new_purchase_disabled(self):
        from django.core.management import call_command, get_commands
        from unittest.mock import patch
        service = self.service()
        intent = dict(gift_code=self.gift.code, quantity=2, mode='inventory')
        q = service.quote(self.user, **intent)
        with patch('apps.gifts.services.purchases.fulfill', side_effect=RuntimeError('local crash')):
            with self.assertRaises(RuntimeError):
                service.purchase(self.user, **intent, price_version=q['price_version'], idempotency_key='crash')
        self.assertIn('reconcile_wallet_spends', get_commands())
        with override_settings(GIFT_PURCHASE_ENABLED=False):
            call_command('reconcile_wallet_spends', apply=True)
            call_command('reconcile_wallet_spends', apply=True)
        self.assertEqual(GiftPurchase.objects.get().status, 'paid')
        self.assertEqual(GiftInventoryLot.objects.get().remaining, 2)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('8.00'))

    def test_authenticated_api_purchase_and_owner_only_readback(self):
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.user)
        intent = dict(gift_code=self.gift.code, quantity=2, mode='inventory')
        response = client.post('/api/gifts/quotes/', intent, format='json')
        self.assertEqual(response.status_code, 200)
        intent.update(price_version=response.data['price_version'], idempotency_key='api-key')
        response = client.post('/api/gifts/purchases/', intent, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['payment_status'], 'paid')
        no = response.data['purchase_no']
        self.assertEqual(client.get('/api/gifts/purchases/by-key/', {'idempotency_key': 'api-key'}).status_code, 200)
        self.assertEqual(client.get('/api/gifts/capabilities/').status_code, 200)
        self.assertEqual(client.get('/api/gifts/purchase-records/').data['count'], 1)
        self.assertEqual(client.get('/api/gifts/transfers/by-key/', {'idempotency_key': 'absent'}).status_code, 404)
        self.assertEqual(client.get('/api/gifts/purchases/' + no + '/').status_code, 200)
        client.force_authenticate(User.objects.create_user('other'))
        self.assertEqual(client.get('/api/gifts/purchases/' + no + '/').status_code, 404)

    def test_inventory_purchase_is_atomic_idempotent_and_has_no_earning(self):
        service = self.service()
        intent = {'gift_code': self.gift.code, 'quantity': 2, 'mode': 'inventory', 'recipient_id': None}
        quote = service.quote(self.user, **intent)
        intent.update(price_version=quote['price_version'], idempotency_key='synthetic-key')
        purchase = service.purchase(self.user, **intent)
        self.assertEqual(purchase.status, 'paid')
        self.assertEqual(service.purchase(self.user, **intent).pk, purchase.pk)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('8.00'))
        self.assertEqual(GiftInventoryLot.objects.get(purchase=purchase).remaining, 2)
        self.assertEqual(GiftPurchase.objects.count(), 1)
        from apps.gifts.models import GiftEarning
        self.assertFalse(GiftEarning.objects.exists())
        intent['quantity'] = 3
        with self.assertRaises(ValidationError):
            service.purchase(self.user, **intent)
