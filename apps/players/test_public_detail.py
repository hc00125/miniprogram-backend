from django.test import SimpleTestCase, TestCase, override_settings
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from datetime import timedelta
from apps.accounts.models import ClientProfile
from django.urls import resolve
from rest_framework.test import APIClient
from apps.players.models import Player
from apps.catalog.models import PlayerType

class PublicDetailRouteTests(SimpleTestCase):
    def test_exact_frontend_route_exists(self):
        self.assertEqual(resolve('/api/player/127/detail').kwargs, {'player_id': 127})

class PublicDetailTests(TestCase):
    def setUp(self):
        self.kind = PlayerType.objects.create(name='detail-test', priority=1)
        self.player = Player.objects.create(name='public-test', player_type=self.kind, contact_wechat='PRIVATE-CONTACT')
        self.client = APIClient()
    def test_public_detail_matches_id(self):
        response = self.client.get('/api/player/%s/detail' % self.player.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['id'], self.player.pk)

    def get_detail(self):
        return self.client.get('/api/player/%s/detail' % self.player.pk)

    def test_missing_is_explicit_business_unavailable(self):
        r = self.client.get('/api/player/99999999/detail')
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()['code'], 'PLAYER_UNAVAILABLE')

    def test_unapproved_disabled_hidden_are_unavailable(self):
        for status in ('pending', 'rejected', 'disabled'):
            self.player.status = status
            self.player.save(update_fields=['status'])
            self.assertEqual(self.get_detail().status_code, 404)
        self.player.status = 'approved'
        self.player.is_publicly_visible = False
        self.player.save()
        self.assertEqual(self.get_detail().status_code, 404)

    @override_settings(FEATURE_DESIGNATE_DISABLED=False)
    def test_resting_and_undesignatable_remain_visible(self):
        for online, designated in ((False, True), (True, False), (False, False)):
            self.player.is_online, self.player.can_be_designated = online, designated
            self.player.save()
            r = self.get_detail()
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()['is_online'], online)
            self.assertEqual(r.json()['can_be_designated'], designated)

    def profile(self):
        user = User.objects.create_user(username='detail-owner')
        self.player.user = user
        self.player.save()
        return ClientProfile.objects.create(user=user, openid='private-openid', nickname='private-name', avatar_url='https://example.com/avatar.png')

    def test_account_suspension_ban_and_expiry(self):
        profile = self.profile()
        for status, until, expected in (
            ('active', None, 200), ('banned', None, 404),
            ('suspended', None, 404), ('suspended', timezone.now()+timedelta(days=1), 404),
            ('suspended', timezone.now()-timedelta(days=1), 200),
        ):
            profile.account_status, profile.account_suspended_until = status, until
            profile.save()
            self.assertEqual(self.get_detail().status_code, expected)
        profile.refresh_from_db()
        self.assertEqual(profile.account_status, 'suspended')

    def test_inactive_auth_user_is_not_public(self):
        profile = self.profile()
        profile.user.is_active = False
        profile.user.save()
        self.assertEqual(self.get_detail().status_code, 404)

    def test_public_whitelist_matches_list_and_has_no_privacy(self):
        self.profile()
        data = self.get_detail().json()
        expected = {'id','name','type_id','type_name','type_priority','price_extra','designated_billing_type_id','designated_billing_type_name','designated_billing_type_priority','avatar_url','bio','audio_intro_url','audio_intro_title','player_type','designated_billing_type','avg_rating','rating_count','total_orders','is_online','presence_online','can_be_designated','status','created_at'}
        self.assertEqual(set(data), expected)
        self.assertEqual(data, self.client.get('/api/player/list').json()[0])
        self.assertNotIn('PRIVATE-CONTACT', str(data))
        self.assertNotIn('private-openid', str(data))

    def test_get_never_authenticates_or_writes_even_with_bad_token(self):
        self.profile()
        self.client.credentials(HTTP_AUTHORIZATION='Bearer invalid-token')
        with CaptureQueriesContext(connection) as queries:
            r = self.get_detail()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(queries.captured_queries)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.player.refresh_from_db()
        self.assertIsNone(self.player.presence_seen_at)

    def test_non_get_disallowed(self):
        self.assertEqual(self.client.post('/api/player/%s/detail' % self.player.pk, {}).status_code, 405)

    @override_settings(FEATURE_DESIGNATE_DISABLED=True)
    def test_designation_feature_gate_is_preserved(self):
        self.assertFalse(self.get_detail().json()['can_be_designated'])

    def test_legacy_model_has_no_archive_dependency(self):
        if any(f.name == 'is_archived' for f in Player._meta.fields):
            self.skipTest('Archive schema exists in development; legacy coverage runs in release')
        self.assertEqual(self.get_detail().status_code, 200)

    def test_archive_semantics_when_schema_present(self):
        if not any(f.name == 'is_archived' for f in Player._meta.fields):
            self.skipTest('Production has no archive schema')
        from apps.players.archive import archive_players
        actor = User.objects.create_user(username='detail-archive-actor')
        archive_players([self.player.pk], actor=actor, reason='isolated detail contract test')
        data = self.get_detail().json()
        self.assertTrue(data['is_archived'])
        self.assertEqual(data['name'], self.player.name)
        self.assertFalse(data['can_be_designated'])
