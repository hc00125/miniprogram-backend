"""A-owned isolated regressions; not independent T/R acceptance."""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import connection, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.kook_integration import binding, outbox
from apps.kook_integration.client import SendResult
from apps.kook_integration.models import KookBinding, KookAuditLog, KookDelivery
from apps.kook_integration.secrets import KookError, bot_key


def player(name):
    user = get_user_model().objects.create_user(name)
    ClientProfile.objects.create(user=user, openid='fake-' + name, nickname=name)
    kind, _ = PlayerType.objects.get_or_create(name='offline-hardening', defaults={'priority': 99})
    return Player.objects.create(name=name, user=user, player_type=kind)


def bound(p, target):
    return KookBinding.objects.create(player=p, bot_key=bot_key(), kook_user_id=target, notifications_enabled=True)


def envelope(revision):
    return dict(schema_version=1, event_id=str(uuid.uuid4()), event_type='public_slots.changed',
                source_key='hardening-revision-' + str(revision), order_id=42, revision=revision,
                occurred_at=timezone.now().isoformat(), expires_at=(timezone.now()+timedelta(minutes=10)).isoformat(),
                fulfillment_mode='public', paid=False, audience='public_channel', safe_snapshot={'status':str(revision)})


@override_settings(KOOK_ENABLED=True)
class SecurityRegressions(TestCase):
    def setUp(self):
        self.owner = player('hardening-owner')
        self.other = player('hardening-other')

    def test_cross_user_cannot_read_confirm_cancel_or_poll_delivery(self):
        c, code = binding.create_challenge(self.owner, 'bind')
        binding.claim_code(code, 'fake-owner', 'proof')
        c.refresh_from_db()
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION='Bearer ' + str(AccessToken.for_user(self.other.user)))
        url = '/api/player/kook-binding/challenges/' + str(c.pk)
        for response in [api.get(url), api.delete(url), api.post(url+'/confirm',
                         {'confirmation_nonce':binding.confirmation_nonce(c),'notifications_enabled':True}, format='json')]:
            self.assertEqual(response.status_code, 404, response.content)
            self.assertEqual(response.json()['code'], 'NOT_FOUND')
            self.assertNotIn('fake-owner', response.content.decode())
        c.refresh_from_db()
        self.assertEqual(c.state, 'awaiting_confirmation')
        b = binding.confirm(self.owner, c.pk, binding.confirmation_nonce(c), True)
        with override_settings(KOOK_SEND_ENABLED=True, KOOK_DM_SEND_ENABLED=True,
                               KOOK_DM_ALLOWLIST=[b.kook_user_id], KOOK_PLAYER_ALLOWLIST=[self.owner.pk]):
            d = outbox.enqueue_test(self.owner, uuid.uuid4(), 'offline-ip')
        self.assertEqual(api.get('/api/player/kook-binding/test-notifications/'+str(d.pk)).status_code,404)

    def test_same_kook_account_conflict_preserves_existing_binding(self):
        existing = bound(self.owner, 'fake-shared')
        c, code = binding.create_challenge(self.other, 'bind')
        binding.claim_code(code, 'fake-shared', 'proof')
        c.refresh_from_db()
        with self.assertRaisesMessage(KookError, 'KOOK_ACCOUNT_CONFLICT'):
            binding.confirm(self.other,c.pk,binding.confirmation_nonce(c),True)
        existing.refresh_from_db()
        self.assertTrue(existing.active)
        self.assertFalse(KookBinding.objects.filter(player=self.other,active=True).exists())

    def test_account_restricted_after_enqueue_prevents_test_dm(self):
        b=bound(self.owner,'fake-owner')
        with override_settings(KOOK_SEND_ENABLED=True,KOOK_DM_SEND_ENABLED=True,
                               KOOK_PLAYER_ALLOWLIST=[self.owner.pk],KOOK_DM_ALLOWLIST=[b.kook_user_id]):
            d=outbox.enqueue_test(self.owner,uuid.uuid4(),'restricted-ip')
            ClientProfile.objects.filter(user=self.owner.user).update(account_status='disabled')
            client=Mock(bot_key='offline-test-bot');client.send.return_value=SendResult('sent',message_id='must-not-send')
            outbox.process_once(client=client)
            client.send.assert_not_called()
            d.refresh_from_db();self.assertEqual(d.last_error_code,'ACCOUNT_RESTRICTED')

    def test_private_intent_does_not_disclose_order_to_other_user(self):
        from apps.kook_integration.navigation import create_intent,resolve_intent
        b=bound(self.owner,'fake-owner')
        token,obj=create_intent(42,'dm',timezone.now()+timedelta(minutes=10),designation_id=1,binding=b)
        resolver=Mock(return_value={'state':'invited','order_no':'fake-order'})
        with override_settings(KOOK_ENTRY_RESOLVER=resolver):
            result=resolve_intent(token,self.other)
            self.assertEqual(result['state'],'forbidden')
            self.assertNotIn('order_no',result)
            resolver.assert_not_called()
            self.assertEqual(resolve_intent(token,self.owner)['order_no'],'fake-order')
            binding.unbind(self.owner,b.version)
            self.assertEqual(resolve_intent(token,self.owner)['state'],'expired')

    def test_unbind_between_claim_and_last_check_prevents_send(self):
        b = bound(self.owner, 'fake-owner')
        with override_settings(KOOK_SEND_ENABLED=True, KOOK_DM_SEND_ENABLED=True,
                               KOOK_DM_ALLOWLIST=[b.kook_user_id], KOOK_PLAYER_ALLOWLIST=[self.owner.pk]):
            d = outbox.enqueue_test(self.owner,uuid.uuid4(),'offline-ip')
            original = outbox.claim_delivery
            def claim_then_revoke(pk):
                claimed = original(pk)
                binding.unbind(self.owner,b.version)
                return claimed
            client = Mock(bot_key='offline-test-bot')
            with patch.object(outbox,'claim_delivery',side_effect=claim_then_revoke):
                outbox.process_once(client=client)
            client.send.assert_not_called()
            d.refresh_from_db()
            self.assertEqual(d.last_error_code,'BINDING_REVOKED')
            self.assertNotEqual(d.status,'sent')

    @override_settings(KOOK_SEND_ENABLED=True, KOOK_CHANNEL_SEND_ENABLED=True,
                       KOOK_TARGET_CONFIG_VERSION='offline', KOOK_VERIFIED_CONFIG_VERSION='offline',
                       KOOK_VERIFIED_BOT_KEY='offline-test-bot', KOOK_VERIFIED_CHANNEL_ID='fake-channel',
                       KOOK_VERIFIED_GUILD_ID='fake-guild', KOOK_ORDER_EVENTS_ENABLED=True, KOOK_TEST_ORDER_IDS=[42], KOOK_ORDER_REVALIDATOR=lambda e,d: True)
    def test_revision_during_send_converges_to_same_remote_message(self):
        with transaction.atomic():
            first = outbox.schedule_delivery(outbox.append_event(envelope(1)),'channel')
        def send(*args, **kwargs):
            if kwargs['operation']=='create':
                with transaction.atomic():
                    outbox.schedule_delivery(outbox.append_event(envelope(2)),'channel')
            return SendResult('sent',message_id='fake-message')
        client=Mock(bot_key='offline-test-bot');client.send.side_effect=send
        outbox.process_once(client=client)
        outbox.process_once(client=client)
        self.assertEqual(client.send.call_count,2)
        self.assertEqual(client.send.call_args.kwargs['operation'],'update')
        self.assertEqual(client.send.call_args.kwargs['message_id'],'fake-message')
        first.stream.refresh_from_db()
        self.assertEqual(first.stream.applied_revision,2)
        self.assertEqual(first.stream.desired_revision,2)
        with transaction.atomic():
            old=outbox.schedule_delivery(first.event,'channel')
        outbox.process_once(client=client)
        self.assertEqual(client.send.call_count,2)


@skipUnless(connection.vendor=='postgresql','Requires disposable PostgreSQL row locks')
@override_settings(KOOK_ENABLED=True)
class PostgreSQLConcurrencyRegressions(TransactionTestCase):
    def parallel(self, functions):
        barrier=threading.Barrier(len(functions))
        def run(fn):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                return fn()
            except KookError as exc:
                return str(exc.detail)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            return list(pool.map(run,functions))

    def test_ip_limit_serializes_different_players(self):
        people=[player('parallel-ip-'+str(i)) for i in range(2)]
        bindings=[bound(p,'fake-ip-'+str(i)) for i,p in enumerate(people)]
        for i in range(4):
            KookAuditLog.objects.create(actor='seed-'+str(i),action='test.enqueue',object_id=binding.digest('ip:shared-ip'))
        original=outbox.schedule_delivery
        def slow_schedule(*args,**kwargs):
            time.sleep(0.15)  # widen the real count/write race; no fake query results
            return original(*args,**kwargs)
        with override_settings(KOOK_SEND_ENABLED=True,KOOK_DM_SEND_ENABLED=True,
                               KOOK_PLAYER_ALLOWLIST=[p.pk for p in people],KOOK_DM_ALLOWLIST=[b.kook_user_id for b in bindings]):
            with patch.object(outbox,'schedule_delivery',side_effect=slow_schedule):
                results=self.parallel([lambda p=p: outbox.enqueue_test(p,uuid.uuid4(),'shared-ip').status for p in people])
        self.assertCountEqual(results,['queued','TEST_RATE_LIMIT'])
        self.assertEqual(KookDelivery.objects.count(),1)

    def test_unbind_races_rebind_without_revoking_new_version(self):
        p=player('parallel-rebind')
        old=bound(p,'fake-old')
        c,code=binding.create_challenge(p,'rebind')
        binding.claim_code(code,'fake-new','proof');c.refresh_from_db()
        def revoke():
            binding.unbind(p,old.version)
            return 'revoked'
        def confirm():
            return binding.confirm(p,c.pk,binding.confirmation_nonce(c),True).kook_user_id
        results=self.parallel([revoke,confirm])
        active=list(KookBinding.objects.filter(player=p,active=True).values_list('kook_user_id',flat=True))
        if 'fake-new' in results:
            self.assertEqual(active,['fake-new'])
            self.assertIn('BINDING_VERSION_CONFLICT',results)
        else:
            self.assertEqual(active,[])
            self.assertCountEqual(results,['revoked','CONFIRMATION_INVALID'])
        old.refresh_from_db();self.assertFalse(old.active)

    def test_concurrent_worker_claim_has_one_lease_and_fences_late_result(self):
        p=player('parallel-worker');b=bound(p,'fake-worker')
        with override_settings(KOOK_SEND_ENABLED=True,KOOK_DM_SEND_ENABLED=True,
                               KOOK_PLAYER_ALLOWLIST=[p.pk],KOOK_DM_ALLOWLIST=[b.kook_user_id]):
            d=outbox.enqueue_test(p,uuid.uuid4(),'worker-ip')
            results=self.parallel([lambda: outbox.claim_delivery(d.pk) for _ in range(2)])
        claimed=[r for r in results if r is not None]
        self.assertEqual(len(claimed),1)
        KookDelivery.objects.filter(pk=d.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        outbox.recover_expired_leases()
        outbox.finish_delivery(claimed[0],SendResult('sent',message_id='late-message'))
        d.refresh_from_db();self.assertEqual(d.status,'unknown')
        d.stream.refresh_from_db();self.assertEqual(d.stream.state,'unknown')
        self.assertEqual(d.stream.applied_revision,0)

    def test_challenge_limit_serializes_same_player(self):
        p=player('parallel-challenge')
        for _ in range(4):
            binding.create_challenge(p,'bind')
        results=self.parallel([lambda: binding.create_challenge(p,'bind')[0].state for _ in range(2)])
        self.assertCountEqual(results,['pending','CHALLENGE_RATE_LIMIT'])

    def test_binding_unique_across_concurrent_users(self):
        people=[player('parallel-bind-'+str(i)) for i in range(2)]
        proofs=[]
        for i,p in enumerate(people):
            c,code=binding.create_challenge(p,'bind')
            binding.claim_code(code,'fake-shared','proof-'+str(i));c.refresh_from_db()
            proofs.append((p,c,binding.confirmation_nonce(c)))
        results=self.parallel([lambda p=p,c=c,n=n: binding.confirm(p,c.pk,n,True).active for p,c,n in proofs])
        self.assertCountEqual(results,[True,'KOOK_ACCOUNT_CONFLICT'])
        self.assertEqual(KookBinding.objects.filter(active=True).count(),1)
