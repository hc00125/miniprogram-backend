from types import SimpleNamespace

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType

from .models import Player, PlayerApplication
from .serializers import PlayerApplicationCreateSerializer


class PlayerApplicationSubmissionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.player_type = PlayerType.objects.create(
            name='申请测试类型',
            priority=1,
            is_active=True,
        )
        self.user = User.objects.create_user(
            username='application-user',
            password='password',
        )
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='application-openid',
            nickname='长清道长',
            nickname_customized=True,
            player_status=ClientProfile.PLAYER_STATUS_NONE,
        )
        self.payload = {
            'name': self.profile.nickname,
            'real_name': '张三',
            'type_id': self.player_type.id,
            'contact_wechat': 'test-wechat',
            'bio': '申请测试',
        }
        self.url = '/api/player/apply'

    def test_name_validation_excludes_current_users_player(self):
        Player.objects.create(
            user=self.user,
            name=self.profile.nickname,
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
        )
        serializer = PlayerApplicationCreateSerializer(
            data=self.payload,
            context={'request': SimpleNamespace(user=self.user)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_approved_player_cannot_submit_second_application(self):
        Player.objects.create(
            user=self.user,
            name=self.profile.nickname,
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
        )
        self.profile.player_status = ClientProfile.PLAYER_STATUS_APPROVED
        self.profile.save(update_fields=['player_status', 'updated_at'])
        self.client.force_authenticate(user=self.user)

        response = self.client.post(self.url, self.payload, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'PLAYER_ALREADY_APPROVED')
        self.assertEqual(response.data['detail'], '您已经是审核通过的陪玩师，无需重复申请')
        self.assertEqual(PlayerApplication.objects.filter(user=self.user).count(), 0)

    def test_pending_application_message_precedes_name_validation(self):
        PlayerApplication.objects.create(
            user=self.user,
            name=self.profile.nickname,
            real_name='张三',
            player_type=self.player_type,
            contact_wechat='test-wechat',
            bio='待审核申请',
            status=PlayerApplication.STATUS_PENDING,
        )
        self.profile.player_status = ClientProfile.PLAYER_STATUS_PENDING
        self.profile.save(update_fields=['player_status', 'updated_at'])
        self.client.force_authenticate(user=self.user)

        response = self.client.post(self.url, self.payload, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'PLAYER_APPLICATION_PENDING')
        self.assertEqual(response.data['detail'], '您的陪玩师申请正在审核中，请勿重复提交')
        self.assertEqual(PlayerApplication.objects.filter(user=self.user).count(), 1)

    def test_other_players_duplicate_name_is_still_rejected(self):
        other_user = User.objects.create_user(username='application-other')
        Player.objects.create(
            user=other_user,
            name=self.profile.nickname,
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
        )
        self.client.force_authenticate(user=self.user)

        response = self.client.post(self.url, self.payload, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            str(response.data['name'][0]),
            '该昵称已被其他陪玩师使用，请换一个',
        )
        self.assertEqual(PlayerApplication.objects.filter(user=self.user).count(), 0)
