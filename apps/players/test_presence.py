from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.catalog.models import PlayerType
from .models import Player


@override_settings(ROOT_URLCONF='config.urls', FEATURE_DESIGNATE_DISABLED=False)
class PlayerPresenceTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='presence-owner')
        kind = PlayerType.objects.create(name='presence-type', priority=0)
        self.player = Player.objects.create(user=self.user, name='presence-player', player_type=kind,
                                           is_online=False, can_accept_orders=False, can_be_designated=False)
        self.url = '/api/player/presence/heartbeat'
        self.client.force_authenticate(self.user)

    def test_heartbeat_records_server_time_without_changing_work_permissions(self):
        now = timezone.now()
        with patch('django.utils.timezone.now', return_value=now):
            result = self.client.post(self.url, {'player_id': 987654321, 'last_seen_at': '2099-01-01'}, format='json')
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.data['tracked'])
        self.assertEqual(result.data['player_id'], self.player.pk)
        self.player.refresh_from_db()
        self.assertEqual(self.player.presence_seen_at, now)
        self.assertFalse(self.player.is_online)
        self.assertFalse(self.player.can_accept_orders)
        self.assertFalse(self.player.can_be_designated)

    def test_anonymous_cannot_record_activity(self):
        self.client.force_authenticate(user=None)
        result = self.client.post(self.url, {}, format='json')
        self.assertIn(result.status_code, (401, 403))
        self.player.refresh_from_db()
        self.assertIsNone(self.player.presence_seen_at)

    def test_non_player_and_disabled_player_are_not_tracked(self):
        boss = User.objects.create_user(username='presence-boss')
        self.client.force_authenticate(boss)
        self.assertEqual(self.client.post(self.url, {}, format='json').data, {'tracked': False})
        self.client.force_authenticate(self.user)
        for state in (Player.STATUS_PENDING, Player.STATUS_REJECTED, Player.STATUS_DISABLED):
            self.player.status = state
            self.player.save(update_fields=['status'])
            self.assertEqual(self.client.post(self.url, {}, format='json').data, {'tracked': False})
        self.player.refresh_from_db()
        self.assertIsNone(self.player.presence_seen_at)

    def test_old_manual_online_does_not_claim_recent_activity(self):
        self.player.is_online = True
        self.player.save(update_fields=['is_online'])
        row = self.client.get('/api/player/list').data[0]
        self.assertIs(row['presence_online'], False)
        self.assertIs(row['is_online'], True)
        self.assertNotIn('presence_seen_at', row)

    def test_serializer_exposes_only_derived_presence(self):
        from .serializers import PlayerSerializer
        self.client.post(self.url, {}, format='json')
        self.player.refresh_from_db()
        data = PlayerSerializer(self.player).data
        self.assertIs(data['presence_online'], True)
        self.assertNotIn('presence_seen_at', data)
        serializer = PlayerSerializer(self.player, data={'presence_online': False}, partial=True)
        self.assertTrue(serializer.is_valid())
        self.assertEqual(serializer.validated_data, {})

    def test_heartbeat_cannot_touch_another_player(self):
        other = Player.objects.create(name='other-presence-player', player_type=self.player.player_type)
        self.client.post(self.url, {'player_id': other.pk}, format='json')
        other.refresh_from_db()
        self.assertIsNone(other.presence_seen_at)

    def test_late_concurrent_request_cannot_regress_newer_timestamp(self):
        now = timezone.now()
        with patch('django.utils.timezone.now', return_value=now + timedelta(seconds=10)):
            self.client.post(self.url, {}, format='json')
        with patch('django.utils.timezone.now', return_value=now):
            self.client.post(self.url, {}, format='json')
        self.player.refresh_from_db()
        self.assertEqual(self.player.presence_seen_at, now + timedelta(seconds=10))

    def test_actual_jwt_authentication_for_current_user(self):
        from rest_framework_simplejwt.tokens import AccessToken
        from apps.accounts.authentication import LenientJWTAuthentication
        from .presence_views import heartbeat
        self.client.force_authenticate(user=None)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(AccessToken.for_user(self.user)))
        with patch.object(heartbeat.cls, 'authentication_classes', [LenientJWTAuthentication]):
            response = self.client.post(self.url, {}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['player_id'], self.player.pk)
        self.player.refresh_from_db()
        self.assertIsNotNone(self.player.presence_seen_at)

    def test_ten_minute_heartbeat_extends_the_window(self):
        now = timezone.now()
        for seconds in (0, 600):
            with patch('django.utils.timezone.now', return_value=now + timedelta(seconds=seconds)):
                self.client.post(self.url, {}, format='json')
        with patch('django.utils.timezone.now', return_value=now + timedelta(seconds=1499)):
            self.assertIs(self.client.get('/api/player/list').data[0]['presence_online'], True)
        with patch('django.utils.timezone.now', return_value=now + timedelta(seconds=1500)):
            self.assertIs(self.client.get('/api/player/list').data[0]['presence_online'], False)

    def test_public_presence_expires_at_15_minutes_and_new_heartbeat_restores_it(self):
        now = timezone.now()
        with patch('django.utils.timezone.now', return_value=now):
            self.client.post(self.url, {}, format='json')
            self.assertIs(self.client.get('/api/player/list').data[0].get('presence_online'), True)
        for elapsed, online in [(899, True), (900, False), (1200, False)]:
            with self.subTest(elapsed=elapsed), patch('django.utils.timezone.now', return_value=now + timedelta(seconds=elapsed)):
                self.assertIs(self.client.get('/api/player/list').data[0].get('presence_online'), online)
        with patch('django.utils.timezone.now', return_value=now + timedelta(seconds=1201)):
            self.client.post(self.url, {}, format='json')
            self.assertIs(self.client.get('/api/player/list').data[0].get('presence_online'), True)
        self.player.refresh_from_db()
        self.assertFalse(self.player.is_online)
