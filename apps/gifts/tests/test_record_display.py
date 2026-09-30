"""Human-readable, participant-only gift histories; GET never moves money."""
import json
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient
from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.gifts.models import Gift, GiftPurchase, GiftTransfer, GiftInventoryLot, GiftTransferAllocation


class GiftRecordDisplayTests(TestCase):
    def setUp(self):
        self.sender = get_user_model().objects.create_user(username='private-sender-login')
        self.receiver = get_user_model().objects.create_user(username='private-receiver-login')
        self.other = get_user_model().objects.create_user(username='unrelated-account')
        self.profile = ClientProfile.objects.create(user=self.sender, openid='private-openid-sender', nickname='送礼老板')
        ClientProfile.objects.create(user=self.receiver, openid='private-openid-receiver', nickname='收礼账号昵称')
        self.player = Player.objects.create(user=self.receiver, name='收礼陪玩', player_type=PlayerType.objects.create(name='record-fixture', priority=0))
        self.gift = Gift.objects.create(name='目录新名称', code='record_fixture_gift', price_diamonds=1)
        self.client = APIClient()
        self.client.force_authenticate(self.sender)

    def record(self, key='record', direct=True):
        purchase = GiftPurchase.objects.create(purchase_no='GP-'+key, buyer=self.sender, gift=self.gift,
            mode='direct' if direct else 'inventory', recipient=self.player if direct else None,
            quantity=2, snapshot={'name':'购买时小星星', 'total_diamonds':2},
            idempotency_key=key, request_digest='a'*64, status='paid')
        transfer = GiftTransfer.objects.create(transfer_no='GT-'+key, sender=self.sender, recipient=self.player,
            gift=self.gift, quantity=2, source='direct' if direct else 'inventory', purchase=purchase if direct else None,
            idempotency_key=key, request_digest='a'*64, commission_snapshot={'commission_rate':'25.00'}, status='delivered')
        if not direct:
            lot = GiftInventoryLot.objects.create(owner=self.sender, gift=self.gift, source='paid',
                source_reference='fixture:'+key, purchase=purchase, quantity=2, remaining=0,
                snapshot={'name':'库存批次小星星'})
            GiftTransferAllocation.objects.create(transfer=transfer, lot=lot, quantity=2, snapshot=lot.snapshot)
        return transfer

    def test_sent_names_show_sender_recipient_and_historical_gift_without_private_identifiers(self):
        row = self.record()
        response = self.client.get('/api/gifts/sent/')
        self.assertEqual(response.status_code, 200)
        data = response.data['results'][0]
        self.assertEqual(data.get('sender_name'), '送礼老板')
        self.assertEqual(data.get('recipient_name'), '收礼陪玩')
        self.assertEqual(data.get('gift_name'), '购买时小星星')
        self.assertEqual(data['transfer_no'], row.transfer_no)
        self.assertEqual(data['quantity'], 2)
        text = json.dumps(response.data, ensure_ascii=False)
        self.assertNotIn('private-openid', text)
        self.assertNotIn('private-sender-login', text)
        self.assertNotIn('private-receiver-login', text)
        self.assertNotIn('commission_rate', text)

    def test_received_shows_same_transfer_and_keeps_participant_filter(self):
        row = self.record()
        self.client.force_authenticate(self.receiver)
        received = self.client.get('/api/gifts/received/')
        self.assertEqual(received.data['count'], 1)
        self.assertEqual(received.data['results'][0].get('sender_name'), '送礼老板')
        self.assertEqual(received.data['results'][0].get('recipient_name'), '收礼陪玩')
        self.assertEqual(self.client.get('/api/gifts/sent/').data['count'], 0)
        self.client.force_authenticate(self.other)
        for route in ['sent', 'received']:
            self.assertEqual(self.client.get('/api/gifts/'+route+'/', {'sender_id':self.sender.pk, 'recipient_id':self.player.pk}).data['count'], 0)
        response = self.client.get('/api/gifts/transfers/by-key/', {'idempotency_key':'record'})
        self.assertEqual(response.status_code, 404)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get('/api/gifts/sent/').status_code, (401, 403))

    def test_inventory_transfer_uses_lot_snapshot_including_inactive_catalog(self):
        self.record(direct=False)
        Gift.objects.filter(pk=self.gift.pk).update(is_active=False, send_enabled=False)
        data = self.client.get('/api/gifts/sent/').data['results'][0]
        self.assertEqual(data.get('gift_name'), '库存批次小星星')
        self.assertEqual(data.get('recipient_name'), '收礼陪玩')

    def test_missing_historical_name_and_sender_profile_have_safe_fallbacks(self):
        self.record()
        GiftPurchase.objects.update(snapshot={'total_diamonds':2})
        self.profile.delete()
        data = self.client.get('/api/gifts/sent/').data['results'][0]
        self.assertEqual(data.get('gift_name'), self.gift.name)
        self.assertEqual(data.get('sender_name'), '未设置昵称的用户')
        self.assertNotIn('private-sender-login', json.dumps(data))

    def test_list_has_bounded_queries_and_no_database_writes(self):
        for i in range(4): self.record(key=str(i), direct=bool(i % 2))
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get('/api/gifts/sent/')
        self.assertEqual(response.data['count'], 4)
        self.assertTrue(all(r.get('gift_name') and r.get('sender_name') and r.get('recipient_name') for r in response.data['results']))
        self.assertLessEqual(len(queries), 5, [q['sql'] for q in queries])
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'ALTER')) for q in queries))


    def test_inventory_and_transfer_include_image_and_acquisition_time(self):
        row = self.record(direct=False)
        Gift.objects.filter(pk=self.gift.pk).update(image='gifts/record-fixture.png')
        GiftInventoryLot.objects.filter(owner=self.sender).update(remaining=1)
        lot = self.client.get('/api/gifts/inventory/').data['results'][0]
        self.assertEqual(lot.get('image_url'), 'http://testserver/media/gifts/record-fixture.png')
        self.assertTrue(lot.get('created_at'))
        sent = self.client.get('/api/gifts/sent/').data['results'][0]
        self.assertEqual(sent.get('image_url'), lot['image_url'])

    def test_earnings_show_allocation_quantity_and_actual_credit_time_without_writes(self):
        from apps.gifts.models import GiftEarning
        from django.utils import timezone
        row = self.record(direct=False)
        allocation = row.allocations.get()
        # A single transfer may span batches: show this earning's allocation, not the whole transfer.
        allocation.quantity = 1
        allocation.save(update_fields=['quantity'])
        credited = timezone.now()
        earning = GiftEarning.objects.create(transfer=row, allocation=allocation, player=self.player,
            earnings_eligible=False, status='available', release_at=credited,
            snapshot={'monetary_accrual':True,'currency':'fish','wallet_destination':'player_wallet','settlement':'immediate','credited_amount':'0.00'})
        self.client.force_authenticate(self.receiver)
        with CaptureQueriesContext(connection) as queries:
            data = self.client.get('/api/gifts/earnings/').data['results'][0]
        self.assertEqual(data.get('gift_name'), '库存批次小星星')
        self.assertEqual(data.get('sender_name'), '送礼老板')
        self.assertEqual(data.get('quantity'), 1)
        self.assertEqual(data.get('credited_at'), credited.isoformat())
        self.assertFalse(data.get('earnings_eligible',True))
        self.assertLessEqual(len(queries), 5)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for q in queries))
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get('/api/gifts/earnings/').data['count'],0)

    def test_historical_pending_earning_does_not_invent_credit_time(self):
        from apps.gifts.models import GiftEarning
        row = self.record()
        GiftEarning.objects.create(transfer=row, player=self.player, snapshot={'monetary_accrual':False})
        self.client.force_authenticate(self.receiver)
        data=self.client.get('/api/gifts/earnings/').data['results'][0]
        self.assertIn('credited_at',data)
        self.assertIsNone(data['credited_at'])


    def test_purchase_history_includes_product_and_destination_without_changing_payment_contract(self):
        self.record(key='direct')
        self.record(key='stock',direct=False)
        Gift.objects.filter(pk=self.gift.pk).update(image='gifts/history.png')
        with CaptureQueriesContext(connection) as queries:
            response=self.client.get('/api/gifts/purchase-records/')
        self.assertEqual(response.status_code,200)
        self.assertLessEqual(len(queries),4)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for q in queries))
        records={r['mode']:r for r in response.data['results']}
        for mode,data in records.items():
            self.assertEqual(data['gift_name'],'购买时小星星')
            self.assertEqual(data['quantity'],2)
            self.assertEqual(data['image_url'],'http://testserver/media/gifts/history.png')
            self.assertTrue(data['created_at'])
            self.assertEqual(data['amount_diamonds'],2)
            self.assertEqual(data['payment_status'],'paid')
        self.assertEqual(records['direct']['recipient_name'],'收礼陪玩')
        self.assertIsNone(records['inventory']['recipient_name'])
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get('/api/gifts/purchase-records/').data['count'],0)

    def test_free_and_unknown_purchase_history_keep_true_payment_state(self):
        self.record()
        GiftPurchase.objects.update(snapshot={'name':'免费礼物','total_diamonds':0},status='unknown')
        data=self.client.get('/api/gifts/purchase-records/').data['results'][0]
        self.assertEqual(data.get('gift_name'),'免费礼物')
        self.assertEqual(data['payment_status'],'unknown')
        self.assertEqual(data['amount_diamonds'],0)

    def test_owner_readback_adds_names_without_changing_transfer_state(self):
        row = self.record(direct=False)
        before = list(GiftTransfer.objects.values())
        response = self.client.get('/api/gifts/transfers/by-key/', {'idempotency_key':'record'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.get('gift_name'), '库存批次小星星')
        self.assertEqual(response.data.get('sender_name'), '送礼老板')
        self.assertEqual(list(GiftTransfer.objects.values()), before)
