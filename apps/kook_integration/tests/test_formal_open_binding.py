from django.test import TestCase, override_settings
from . import test_webhook
from apps.kook_integration.models import KookInboundEvent

@override_settings(KOOK_ENABLED=True)
class OpenBindingReceptionTests(TestCase):
    post = test_webhook.WebhookTests.post
    payload = test_webhook.WebhookTests.payload

    def test_none_accepts_both_types_but_empty_and_nonmember_deny(self):
        for allowed, expected in ((None,1), ([],0), (['other-author'],0), (['fake-author'],1)):
            for kind in (1,9):
                with self.subTest(allowed=allowed,kind=kind), override_settings(KOOK_BINDING_USER_ALLOWLIST=allowed):
                    before=KookInboundEvent.objects.count()
                    payload=self.payload('msg-'+str(before)+'-'+str(kind)+'-'+str(allowed))
                    payload['d']['type']=kind
                    self.assertEqual(self.post(payload).status_code,200)
                    self.assertEqual(KookInboundEvent.objects.count()-before,expected)

    def test_open_binding_still_rejects_bad_token_and_noncommand(self):
        with override_settings(KOOK_BINDING_USER_ALLOWLIST=None):
            payload=self.payload('bad-token'); payload['d']['verify_token']='invalid'
            self.assertEqual(self.post(payload).status_code,403)
            payload=self.payload('bare-code'); payload['d']['content']='ABCDEFGHJK'
            self.assertEqual(self.post(payload).status_code,200)
            self.assertFalse(KookInboundEvent.objects.exists())
