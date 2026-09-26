from datetime import timedelta
from django.test import TestCase
from django.utils import timezone

class NavigationTests(TestCase):
    def test_expired_missing_and_default_fail_closed(self):
        from apps.kook_integration.navigation import create_intent, resolve_intent
        token,obj=create_intent(order_id=42,scope='channel',expires_at=timezone.now()+timedelta(minutes=10))
        self.assertNotIn(token,obj.digest)
        self.assertEqual(obj.url_link,'')
        self.assertEqual(resolve_intent(token,None)['state'],'forbidden')
        obj.expires_at=timezone.now()-timedelta(seconds=1);obj.save()
        self.assertEqual(resolve_intent(token,None)['state'],'expired')
        self.assertEqual(resolve_intent('unknown',None)['state'],'expired')

    def test_provider_path_is_fixed_and_bad_link_rejected(self):
        from apps.kook_integration.navigation import create_intent
        calls=[]
        def provider(**kwargs):
            calls.append(kwargs)
            return 'https://evil.invalid/token'
        token,obj=create_intent(order_id=42,scope='channel',expires_at=timezone.now()+timedelta(minutes=10),link_provider=provider)
        self.assertEqual(calls[0]['path'],'pages/player/grab/index')
        self.assertEqual(calls[0]['query'],'kook_intent='+token)
        self.assertEqual(obj.url_link,'')
