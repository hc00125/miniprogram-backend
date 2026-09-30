import uuid
from datetime import timedelta
from unittest.mock import Mock
from django.test import TestCase, override_settings
from django.db import transaction
from django.utils import timezone
from apps.kook_integration.tests.test_binding import BindingTests

class WorkerCommandTests(TestCase):
    def test_target_check_never_marks_permission_verified(self):
        import io
        from django.core.management import call_command, CommandError
        from unittest.mock import patch
        with patch('apps.kook_integration.client.KookClient.inspect_target', return_value={'identity_ok':True,'channel_ok':True,'permissions_verified':False}):
            output=io.StringIO()
            with override_settings(KOOK_CANDIDATE_GUILD_ID='fake-guild', KOOK_CANDIDATE_CHANNEL_ID='fake-channel'):
                call_command('check_kook_targets',read_only=True,stdout=output)
            self.assertIn('permissions_verified',output.getvalue())
            self.assertIn('false',output.getvalue())

    def test_once_disabled_fails_without_network(self):
        from django.core.management import call_command, CommandError
        with self.assertRaisesMessage(CommandError,'KOOK_DISABLED'):
            call_command('process_kook_outbox',once=True)

class ClientTests(TestCase):
    def test_success_requires_business_code_and_msg_id_and_timeout_unknown(self):
        from apps.kook_integration.client import KookClient
        import requests
        transport=Mock()
        transport.post.return_value=Mock(status_code=200, headers={}, json=lambda:{'code':0,'data':{'msg_id':'fake-msg'}})
        client=KookClient(token='offline',transport=transport)
        result=client.send('dm','fake-user','[]')
        self.assertEqual(result.status,'sent')
        self.assertFalse(transport.post.call_args.kwargs['allow_redirects'])
        self.assertEqual(transport.post.call_args.kwargs['json']['type'],10)
        transport.post.return_value.json=lambda:{'code':0,'data':{}}
        self.assertEqual(client.send('dm','fake-user','[]').status,'unknown')
        transport.post.side_effect=requests.Timeout()
        self.assertEqual(client.send('dm','fake-user','[]').status,'unknown')

    def test_rate_limit_and_permission(self):
        from apps.kook_integration.client import KookClient
        transport=Mock()
        transport.post.return_value=Mock(status_code=429,headers={'X-Rate-Limit-Reset':'1200'},json=lambda:{})
        result=KookClient(token='offline',transport=transport).send('channel','fake-channel','[]')
        self.assertEqual(result.status,'retry')
        self.assertGreaterEqual(result.retry_after,1.2)
        transport.post.return_value.status_code=403
        self.assertEqual(KookClient(token='offline',transport=transport).send('dm','fake','[]').status,'failed')

class OutboxTests(BindingTests):
    def test_test_delivery_idempotent_and_unknown_not_retried(self):
        from apps.accounts.models import ClientProfile
        ClientProfile.objects.create(user=self.player.user, openid='offline-outbox')
        from apps.kook_integration import binding, outbox
        from apps.kook_integration.client import SendResult
        c,code=binding.create_challenge(self.player,'bind')
        binding.claim_code(code,'fake-user','e');c.refresh_from_db()
        b=binding.confirm(self.player,c.pk,binding.confirmation_nonce(c),True)
        rid=uuid.uuid4()
        with override_settings(KOOK_ENABLED=True,KOOK_SEND_ENABLED=True,KOOK_DM_SEND_ENABLED=True,KOOK_DM_ALLOWLIST=['fake-user'],KOOK_PLAYER_ALLOWLIST=[self.player.pk]):
            d=outbox.enqueue_test(self.player,rid,'127.0.0.1')
            self.assertEqual(outbox.enqueue_test(self.player,rid,'127.0.0.1').pk,d.pk)
            client=Mock(bot_key='offline-test-bot');client.send.return_value=SendResult('unknown','TRANSPORT_UNKNOWN')
            outbox.process_once(client=client)
            d.refresh_from_db();self.assertEqual(d.status,'unknown')
            outbox.process_once(client=client)
            self.assertEqual(client.send.call_count,1)

    def test_append_event_rollback_and_privacy(self):
        from apps.kook_integration.outbox import append_event
        from apps.kook_integration.models import KookOutboxEvent
        envelope={'schema_version':1,'event_id':str(uuid.uuid4()),'event_type':'public_slots.opened','source_key':'offline-source','order_id':42,'designation_id':None,'revision':1,'occurred_at':timezone.now().isoformat(),'expires_at':(timezone.now()+timedelta(minutes=10)).isoformat(),'fulfillment_mode':'public','paid':False,'audience':'public_channel','safe_snapshot':{'published_at':'today','participant_count':2,'player_type_label':'普通','boss_note':'联系12345678901'}}
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                append_event(envelope)
                raise RuntimeError('rollback')
        self.assertFalse(KookOutboxEvent.objects.exists())
        envelope['safe_snapshot']['phone']='not-allowed'
        with self.assertRaises(ValueError):
            append_event(envelope)
