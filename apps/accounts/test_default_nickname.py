from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.catalog.models import PlayerType
from apps.players.models import PlayerApplication

from .models import ClientProfile
from .nicknames import DEFAULT_NICKNAME_PREFIX, default_nickname_candidates


@override_settings(DEBUG=True, ENABLE_DEV_OPENID_LOGIN=True, SECRET_KEY='default-nickname-test-secret')
class DefaultNicknameTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_openid_candidate_is_stable_and_not_raw_openid(self):
        openid = 'openid-user-1234567890'
        first = next(default_nickname_candidates(openid))
        second = next(default_nickname_candidates(openid))

        self.assertEqual(first, second)
        self.assertTrue(first.startswith(DEFAULT_NICKNAME_PREFIX))
        self.assertEqual(len(first.removeprefix(DEFAULT_NICKNAME_PREFIX)), 10)
        self.assertNotIn(openid[-10:], first)

    def test_different_openids_receive_different_candidates(self):
        first = next(default_nickname_candidates('openid-user-a'))
        second = next(default_nickname_candidates('openid-user-b'))
        self.assertNotEqual(first, second)

    def test_new_wechat_login_creates_openid_derived_default_nickname(self):
        openid = 'openid-login-test'
        expected = next(default_nickname_candidates(openid))

        response = self.client.post(
            '/api/client/wechat-login',
            {'openid': openid, 'nickname': '微信用户'},
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['profile']['nickname'], expected)
        self.assertFalse(response.data['profile']['nickname_customized'])
        profile = ClientProfile.objects.get(openid=openid)
        self.assertEqual(profile.nickname, expected)
        self.assertFalse(profile.nickname_customized)

    def test_existing_user_keeps_same_default_nickname_on_relogin(self):
        openid = 'openid-repeat-login'
        first = self.client.post('/api/client/wechat-login', {'openid': openid}, format='json')
        second = self.client.post('/api/client/wechat-login', {'openid': openid}, format='json')

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data['profile']['nickname'], second.data['profile']['nickname'])
        self.assertEqual(ClientProfile.objects.filter(openid=openid).count(), 1)

    def test_short_digest_conflict_uses_longer_candidate(self):
        target_openid = 'openid-collision-target'
        candidates = list(default_nickname_candidates(target_openid))
        occupied_user = User.objects.create_user(username='occupied-default-name')
        ClientProfile.objects.create(
            user=occupied_user,
            openid='occupied-openid',
            nickname=candidates[0],
        )

        response = self.client.post(
            '/api/client/wechat-login',
            {'openid': target_openid},
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['profile']['nickname'], candidates[1])


class PlayerApplicationCustomNicknameTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.player_type = PlayerType.objects.create(name='自定义昵称测试类型', priority=1, is_active=True)
        self.user = User.objects.create_user(username='nickname-applicant')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='nickname-applicant-openid',
            nickname='微信用户-ABC1234567',
            nickname_customized=False,
        )
        self.client.force_authenticate(user=self.user)
        self.payload = {
            'name': self.profile.nickname,
            'real_name': '张三',
            'type_id': self.player_type.id,
            'contact_wechat': 'nickname-test-wechat',
            'bio': '自定义昵称申请测试',
        }

    def test_default_nickname_cannot_submit_player_application(self):
        response = self.client.post('/api/player/apply', self.payload, format='json')

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('先设置公开昵称', str(response.data))
        self.assertFalse(PlayerApplication.objects.filter(user=self.user).exists())
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.player_status, ClientProfile.PLAYER_STATUS_NONE)

    def test_application_uses_current_account_nickname(self):
        self.profile.nickname = '自定义陪玩昵称'
        self.profile.nickname_customized = True
        self.profile.save(update_fields=['nickname', 'nickname_customized'])
        payload = {**self.payload, 'name': '客户端伪造昵称'}

        response = self.client.post('/api/player/apply', payload, format='json')

        self.assertEqual(response.status_code, 201, response.data)
        application = PlayerApplication.objects.get(user=self.user)
        self.assertEqual(application.name, '自定义陪玩昵称')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.player_status, ClientProfile.PLAYER_STATUS_PENDING)
