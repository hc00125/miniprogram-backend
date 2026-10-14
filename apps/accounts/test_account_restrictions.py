from datetime import timedelta

from django.contrib import admin
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.catalog.models import PlayerType
from apps.players.models import Player

from .admin import ClientProfileAdmin
from .models import AccountRestrictionLog, ClientProfile


class AccountRestrictionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='restricted-account-user')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='restricted-account-openid',
            nickname='账户限制测试用户',
            nickname_customized=True,
        )
        self.player_type = PlayerType.objects.create(
            name='账户限制测试陪玩类型',
            priority=1,
            is_active=True,
        )
        self.player = Player.objects.create(
            user=self.user,
            name=self.profile.nickname,
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
            is_online=False,
        )
        self.client.force_authenticate(user=self.user)

    def restrict(self, status, *, until=None, reason='恶意下单测试限制'):
        self.profile.account_status = status
        self.profile.account_suspended_until = until
        self.profile.account_restriction_reason = reason
        self.profile.save(update_fields=[
            'account_status', 'account_suspended_until',
            'account_restriction_reason', 'updated_at',
        ])

    def test_suspended_account_can_read_but_cannot_modify_boss_business(self):
        self.restrict(
            ClientProfile.ACCOUNT_STATUS_SUSPENDED,
            until=timezone.now() + timedelta(days=3),
        )

        read_response = self.client.get('/api/boss/cart')
        write_response = self.client.post('/api/boss/cart', {}, format='json')

        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(write_response.status_code, 423)
        self.assertEqual(write_response.data['code'], 'ACCOUNT_SUSPENDED')
        self.assertIn('恶意下单测试限制', write_response.data['detail'])

    def test_banned_account_cannot_enable_player_online_status(self):
        self.restrict(ClientProfile.ACCOUNT_STATUS_BANNED)

        response = self.client.post('/api/player/online-status', {'is_online': True}, format='json')

        self.assertEqual(response.status_code, 423)
        self.assertEqual(response.data['code'], 'ACCOUNT_BANNED')
        self.player.refresh_from_db()
        self.assertFalse(self.player.is_online)

    def test_expired_suspension_auto_restores_before_operation(self):
        expired_at = timezone.now() - timedelta(minutes=1)
        self.restrict(
            ClientProfile.ACCOUNT_STATUS_SUSPENDED,
            until=expired_at,
        )

        response = self.client.post('/api/boss/cart', {}, format='json')

        self.assertEqual(response.status_code, 400)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.account_status, ClientProfile.ACCOUNT_STATUS_ACTIVE)
        self.assertIsNone(self.profile.account_suspended_until)
        self.assertEqual(self.profile.account_restriction_reason, '')
        log = AccountRestrictionLog.objects.get(profile=self.profile)
        self.assertEqual(log.from_status, ClientProfile.ACCOUNT_STATUS_SUSPENDED)
        self.assertEqual(log.to_status, ClientProfile.ACCOUNT_STATUS_ACTIVE)
        self.assertIsNone(log.operator)

    def test_restricted_player_is_hidden_from_public_list(self):
        self.restrict(ClientProfile.ACCOUNT_STATUS_BANNED)

        response = self.client.get('/api/player/list')

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.player.id, [item['id'] for item in response.data])

    def test_admin_restriction_records_operator_and_forces_player_offline(self):
        administrator = User.objects.create_superuser(
            username='account-restriction-admin',
            email='admin@example.com',
            password='password',
        )
        self.player.is_online = True
        self.player.save(update_fields=['is_online', 'updated_at'])
        request = RequestFactory().post('/admin/accounts/clientprofile/')
        request.user = administrator
        model_admin = ClientProfileAdmin(ClientProfile, admin.site)

        self.profile.account_status = ClientProfile.ACCOUNT_STATUS_SUSPENDED
        self.profile.account_suspended_until = timezone.now() + timedelta(days=7)
        self.profile.account_restriction_reason = '多次恶意下单，占用陪玩资源'
        model_admin.save_model(request, self.profile, form=None, change=True)

        self.profile.refresh_from_db()
        self.player.refresh_from_db()
        log = AccountRestrictionLog.objects.get(profile=self.profile)
        self.assertEqual(self.profile.account_restricted_by, administrator)
        self.assertIsNotNone(self.profile.account_restricted_at)
        self.assertEqual(log.from_status, ClientProfile.ACCOUNT_STATUS_ACTIVE)
        self.assertEqual(log.to_status, ClientProfile.ACCOUNT_STATUS_SUSPENDED)
        self.assertEqual(log.operator, administrator)
        self.assertFalse(self.player.is_online)
