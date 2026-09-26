"""Explicit isolated PostgreSQL authorization races; unexpected SQL errors fail."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch
from django.db import connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.contrib.auth.models import User
from rest_framework.exceptions import ValidationError
from apps.gifts.tests import test_purchases as fixtures
from apps.gifts.services import purchases
from apps.gifts.models import Gift, GiftPurchase, PlayerGiftConfig
from apps.accounts.models import ClientProfile
from apps.players.models import Player
from apps.catalog.models import PlayerType
from apps.wallet.models import WalletSpendAttempt
from decimal import Decimal

@override_settings(WECHAT_VIRTUALPAY_ENV=1,GIFT_PURCHASE_ENABLED=True,GIFT_TRANSACTION_POLICY={'version':'pg-safety','platform_approved':True,'max_quantity':10,'max_diamonds':1000,'daily_diamonds':2000,'recipient_ids':[]})
class AuthorizationRaceTests(TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor,'postgresql')
        fixtures.PurchaseFlowTests.setUp(self)
        receiver=User.objects.create_user('pg-receiver')
        self.receiver=receiver
        ClientProfile.objects.create(user=receiver,openid='pg-receiver',nickname='pg-receiver')
        self.player=Player.objects.create(user=receiver,name='pg-player',player_type=PlayerType.objects.create(name='pg-type',priority=1))
        self.config=PlayerGiftConfig(player=self.player,gift=self.gift,commission_rate=Decimal('0'))
        self.config.save(_audited=True)

    def blocked_writer_race(self, update):
        """Hold actual authorization locks; UPDATE must wait, not deadlock/timeout."""
        entered=Event(); finished=Event()
        def writer():
            connections.close_all()
            try:
                with transaction.atomic():
                    with connection.cursor() as c: c.execute("SET LOCAL lock_timeout='3s'")
                    entered.set(); update()
                finished.set()
            finally: connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                purchases.lock_authorization(self.user,self.gift.code,self.player.pk)
                future=pool.submit(writer)
                self.assertTrue(entered.wait(2))
                self.assertFalse(finished.wait(.15),'configuration write bypassed authorization lock')
            future.result(timeout=5)
        self.assertTrue(finished.is_set())

    def test_price_and_delisting_serialize(self):
        self.blocked_writer_race(lambda:Gift.objects.filter(pk=self.gift.pk).update(price_diamonds=99,is_active=False))
    def test_config_revocation_serializes(self):
        self.blocked_writer_race(lambda:PlayerGiftConfig.objects.filter(pk=self.config.pk).update(is_enabled=False,version=2))
    def test_buyer_ban_serializes(self):
        self.blocked_writer_race(lambda:User.objects.filter(pk=self.user.pk).update(is_active=False))
    def test_recipient_ban_serializes(self):
        self.blocked_writer_race(lambda:ClientProfile.objects.filter(user=self.receiver).update(account_status='banned'))

    def test_real_inventory_send_serializes_config_revoke(self):
        from apps.gifts.services import transfers
        from apps.gifts.models import GiftInventoryLot
        p={'version':'pg-safety','platform_approved':True,'max_quantity':10,'max_diamonds':1000,'daily_diamonds':2000,'recipient_ids':[self.player.pk]}
        entered=Event(); finished=Event()
        def writer():
            connections.close_all()
            try:
                with transaction.atomic():
                    with connection.cursor() as c:c.execute("SET LOCAL lock_timeout='3s'")
                    entered.set()
                    PlayerGiftConfig.objects.filter(pk=self.config.pk).update(is_enabled=False,version=2)
                finished.set()
            finally:connections.close_all()
        original=transfers.resolve_commission
        with override_settings(GIFT_TRANSACTION_POLICY=p,GIFT_INVENTORY_SEND_ENABLED=True),ThreadPoolExecutor(max_workers=1) as pool:
            data=dict(gift_code=self.gift.code,quantity=1,mode='inventory')
            q=purchases.quote(self.user,**data)
            purchases.purchase(self.user,**data,price_version=q['price_version'],idempotency_key='inventory')
            futures=[]
            def checkpoint(*args,**kwargs):
                result=original(*args,**kwargs)
                futures.append(pool.submit(writer))
                self.assertTrue(entered.wait(2));self.assertFalse(finished.wait(.15))
                return result
            with patch('apps.gifts.services.transfers.resolve_commission',side_effect=checkpoint):
                sent=transfers.transfer(self.user,gift_code=self.gift.code,quantity=1,recipient_id=self.player.pk,commission_version=1,idempotency_key='send')
            futures[0].result(timeout=5)
        self.assertEqual(sent.commission_snapshot['version'],1)
        self.assertEqual(sent.commission_snapshot['commission_rate'],'0.00')
        self.assertEqual(GiftInventoryLot.objects.get().remaining,0)
        self.wallet.refresh_from_db();self.assertEqual(self.wallet.balance,Decimal('9'))

    def test_real_purchase_prepare_blocks_concurrent_delisting(self):
        entered=Event();finished=Event()
        def writer():
            connections.close_all()
            try:
                with transaction.atomic():
                    with connection.cursor() as c:c.execute("SET LOCAL lock_timeout='3s'")
                    entered.set();Gift.objects.filter(pk=self.gift.pk).update(price_diamonds=99,is_active=False)
                finished.set()
            finally:connections.close_all()
        data=dict(gift_code=self.gift.code,quantity=1,mode='inventory')
        q=purchases.quote(self.user,**data); original=purchases.quote
        with ThreadPoolExecutor(max_workers=1) as pool:
            futures=[]
            def checkpoint(*args,**kwargs):
                result=original(*args,**kwargs)
                if not futures:
                    futures.append(pool.submit(writer));self.assertTrue(entered.wait(2));self.assertFalse(finished.wait(.15))
                return result
            with patch('apps.gifts.services.purchases.quote',side_effect=checkpoint):
                try:purchases.purchase(self.user,**data,price_version=q['price_version'],idempotency_key='real-race')
                except ValidationError:pass  # Revocation won before dispatch authorization.
            futures[0].result(timeout=5)
        record=GiftPurchase.objects.get(); self.wallet.refresh_from_db()
        self.assertIn(record.status,('paid','failed'))
        self.assertEqual(record.snapshot['unit_diamonds']*record.quantity,record.snapshot['total_diamonds'])
        self.assertEqual(self.wallet.balance,Decimal('9') if record.status=='paid' else Decimal('10'))

    def test_concurrent_price_commit_before_first_dispatch_is_rejected(self):
        data=dict(gift_code=self.gift.code,quantity=1,mode='inventory')
        q=purchases.quote(self.user,**data)
        data.update(price_version=q['price_version'],idempotency_key='pg-price')
        with patch('apps.wallet.spend_service.execute',side_effect=RuntimeError('prepared')):
            with self.assertRaises(RuntimeError): purchases.purchase(self.user,**data)
        def writer():
            connections.close_all()
            try: Gift.objects.filter(pk=self.gift.pk).update(price_diamonds=99,is_active=False)
            finally: connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool: pool.submit(writer).result(timeout=5)
        with self.assertRaises(ValidationError): purchases.purchase(self.user,**data)
        attempt=WalletSpendAttempt.objects.get(); record=GiftPurchase.objects.get()
        self.assertEqual(attempt.status,'failed'); self.assertEqual(attempt.reserved_amount,0)
        self.assertEqual(record.snapshot['unit_diamonds']*record.quantity,record.snapshot['total_diamonds'])
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance,Decimal('10'))
