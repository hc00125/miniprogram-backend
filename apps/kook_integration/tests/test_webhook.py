import json, zlib
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.test import RequestFactory
from apps.kook_integration.models import KookInboundEvent

@override_settings(KOOK_ENABLED=True)
class WebhookTests(TestCase):
    def post(self, payload, compressed=False):
        from apps.kook_integration.webhook import webhook
        body = json.dumps(payload).encode()
        if compressed:
            body = zlib.compress(body)
        return webhook(RequestFactory().post('/api/integrations/kook/webhook', body, content_type='application/json'))

    def payload(self, msg='a', sn=1):
        return {'s':0, 'sn':sn, 'd': {'verify_token':'fake-verify','channel_type':'PERSON','type':1,'author_id':'fake-author','msg_id':msg,'msg_timestamp':1234,'content':'bind ABCDEFGHJK','extra':{'author':{'bot':False,'username':'name'}}}}

    def test_malformed_nested_objects_are_bad_requests(self):
        payload=self.payload();payload['d']['extra']='not-an-object'
        self.assertEqual(self.post(payload).status_code,400)

    def test_encrypted_challenge_and_no_plaintext_downgrade(self):
        import base64
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        from django.conf import settings
        payload={'s':0,'d':{'verify_token':'fake-verify','channel_type':'WEBHOOK_CHALLENGE','type':255,'challenge':'加密测试'}}
        values=dict(settings.KOOK_TEST_SECRETS,encrypt_key='offline-key')
        with override_settings(KOOK_ENCRYPTION_ENABLED=True,KOOK_TEST_SECRETS=values):
            key=b'offline-key'.ljust(32,b'\0');iv=b'0'*16
            pad=PKCS7(128).padder();body=json.dumps(payload).encode()
            padded=pad.update(body)+pad.finalize()
            encryptor=Cipher(algorithms.AES(key),modes.CBC(iv)).encryptor()
            ciphertext=encryptor.update(padded)+encryptor.finalize()
            outer={'encrypt':base64.b64encode(iv+base64.b64encode(ciphertext)).decode()}
            self.assertEqual(json.loads(self.post(outer,True).content),{'challenge':'加密测试'})
            self.assertEqual(self.post(payload).status_code,400)
            self.assertEqual(self.post({'encrypt':'bad'}).status_code,400)

    def test_compressed_bomb_is_rejected(self):
        from apps.kook_integration.webhook import webhook
        body=zlib.compress(b'x'*(1024*1024+1))
        response=webhook(RequestFactory().post('/',body,content_type='application/json'))
        self.assertEqual(response.status_code,413)

    def test_challenge_and_token(self):
        p = {'s':0,'d':{'verify_token':'fake-verify','channel_type':'WEBHOOK_CHALLENGE','type':255,'challenge':'abc'}}
        self.assertEqual(json.loads(self.post(p, True).content), {'challenge':'abc'})
        p['d']['verify_token']='bad'
        self.assertEqual(self.post(p).status_code, 403)

    def test_replay_sn_wrap_and_secret_minimization(self):
        self.assertEqual(self.post(self.payload()).status_code,200)
        self.assertEqual(self.post(self.payload()).status_code,200)
        self.assertEqual(self.post(self.payload('b')).status_code,200)
        self.assertEqual(KookInboundEvent.objects.count(),2)
        self.assertNotIn('verify_token', str(list(KookInboundEvent.objects.values('payload'))))

    def test_ignore_group_system_bot(self):
        for field,value in [('channel_type','GROUP'),('type',255)]:
            p=self.payload(); p['d'][field]=value
            self.assertEqual(self.post(p).status_code,200)
        p=self.payload(); p['d']['extra']['author']['bot']=True
        self.assertEqual(self.post(p).status_code,200)
        self.assertEqual(KookInboundEvent.objects.count(),0)

    def test_person_kmarkdown_exact_bind_is_durable_and_replay_safe(self):
        # KOOK's real client emits type 9 even for an unformatted DM.
        p = self.payload('kmarkdown-dm')
        p['d']['type'] = 9
        self.assertEqual(self.post(p).status_code, 200)
        self.assertEqual(self.post(p).status_code, 200)
        self.assertEqual(KookInboundEvent.objects.count(), 1)
        self.assertEqual(KookInboundEvent.objects.get().state, 'queued')

    def test_kmarkdown_does_not_relax_binding_security_filters(self):
        for field, value in [('channel_type', 'GROUP'), ('content', '**bind ABCDEFGHJK**'), ('content', 'bind ABCDEFGHJK\n')]:
            p = self.payload('rejected'); p['d']['type'] = 9; p['d'][field] = value
            self.assertEqual(self.post(p).status_code, 200)
        p = self.payload('bot'); p['d']['type'] = 9; p['d']['extra']['author']['bot'] = True
        self.assertEqual(self.post(p).status_code, 200)
        with override_settings(KOOK_BINDING_USER_ALLOWLIST=[]):
            p = self.payload('not-allowed'); p['d']['type'] = 9
            self.assertEqual(self.post(p).status_code, 200)
        self.assertEqual(KookInboundEvent.objects.count(), 0)

    def test_db_failure_does_not_ack(self):
        from django.db import DatabaseError
        with patch.object(KookInboundEvent.objects, 'get_or_create', side_effect=DatabaseError):
            self.assertEqual(self.post(self.payload()).status_code,503)
