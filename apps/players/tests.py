from unittest.mock import Mock

from django.contrib import admin
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from rest_framework.test import APIClient

from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType

from .admin import PlayerApplicationAdmin
from .models import Player, PlayerApplication


class UpdateOnlineStatusTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.player_type = PlayerType.objects.create(name='测试类型', priority=0, is_active=True)
        self.user = User.objects.create_user(username='testplayer', password='testpass123')
        self.player = Player.objects.create(
            user=self.user,
            name='测试陪玩',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
            is_online=False,
        )
        self.url = '/api/player/online-status'

    def test_approved_player_set_online(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, {'is_online': True}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['is_online'], True)
        self.assertEqual(response.data['status'], '在线')
        self.player.refresh_from_db()
        self.assertTrue(self.player.is_online)

    def test_approved_player_set_offline(self):
        self.player.is_online = True
        self.player.save()
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, {'is_online': False}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['is_online'], False)
        self.assertEqual(response.data['status'], '离线')
        self.player.refresh_from_db()
        self.assertFalse(self.player.is_online)

    def test_unauthenticated_user_blocked(self):
        response = self.client.post(self.url, {'is_online': True}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_non_approved_player_blocked(self):
        self.player.status = Player.STATUS_PENDING
        self.player.save()
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, {'is_online': True}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_invalid_is_online_type(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, {'is_online': 'yes'}, format='json')
        self.assertEqual(response.status_code, 400)


class PlayerApplicationAdminTests(TestCase):
    def test_reject_applications_syncs_client_profile_status(self):
        admin_user = User.objects.create_superuser(
            username='admin',
            email='admin@example.com',
            password='password',
        )
        applicant = User.objects.create_user(
            username='applicant',
            password='password',
        )
        profile = ClientProfile.objects.create(
            user=applicant,
            openid='test-openid',
            nickname='测试用户',
            player_status=ClientProfile.PLAYER_STATUS_PENDING,
        )
        application = PlayerApplication.objects.create(
            user=applicant,
            name='测试陪玩',
            contact_wechat='test-wechat',
            status=PlayerApplication.STATUS_PENDING,
        )

        request = RequestFactory().post('/admin/players/playerapplication/')
        request.user = admin_user
        model_admin = PlayerApplicationAdmin(PlayerApplication, admin.site)
        model_admin.message_user = Mock()

        model_admin.reject_applications(
            request,
            PlayerApplication.objects.filter(pk=application.pk),
        )

        application.refresh_from_db()
        profile.refresh_from_db()

        self.assertEqual(application.status, PlayerApplication.STATUS_REJECTED)
        self.assertEqual(profile.player_status, ClientProfile.PLAYER_STATUS_REJECTED)
        self.assertEqual(application.reviewed_by, admin_user)
        self.assertIsNotNone(application.reviewed_at)
