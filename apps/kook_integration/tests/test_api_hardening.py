from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken
from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType
from apps.players.models import Player
from apps.kook_integration.models import KookBinding
from apps.kook_integration.secrets import bot_key


class PreferencesTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user('preferences-test')
        self.profile = ClientProfile.objects.create(user=user, openid='preferences-test')
        self.player = Player.objects.create(name='preferences-test', user=user,
                                           player_type=PlayerType.objects.create(name='preferences-test', priority=99))
        self.binding = KookBinding.objects.create(player=self.player, bot_key=bot_key(),
                                                 kook_user_id='private-kook-user', notifications_enabled=True)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION='Bearer ' + str(AccessToken.for_user(user)))
        self.url = '/api/player/kook-binding'

    def test_post_alias_retains_patch_validation_permissions_and_own_binding(self):
        for method in ('post', 'patch'):
            send = getattr(self.api, method)
            response = send(self.url, {'notifications_enabled': False}, format='json')
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()['notifications_enabled'])
            self.binding.refresh_from_db()
            self.assertFalse(self.binding.notifications_enabled)
            self.assertNotIn('private-kook-user', str(response.json()))
            self.assertEqual(send(self.url, {'notifications_enabled': True, 'player_id': 999}, format='json').status_code, 400)
            self.assertEqual(send(self.url, {'notifications_enabled': True}, format='json').status_code, 200)
        self.profile.account_status = 'banned'
        self.profile.save(update_fields=['account_status'])
        for method in ('post', 'patch'):
            self.assertEqual(getattr(self.api, method)(self.url, {'notifications_enabled': False}, format='json').status_code, 403)
        self.api.credentials()
        for method in ('post', 'patch'):
            self.assertEqual(getattr(self.api, method)(self.url, {'notifications_enabled': False}, format='json').status_code, 401)
    def test_mode_flags_are_booleans_not_credentials_or_readiness(self):
        for enabled, send_enabled in ((False, False), (True, False), (True, True), (False, True)):
            with override_settings(KOOK_ENABLED=enabled, KOOK_SEND_ENABLED=send_enabled):
                data = self.api.get(self.url).json()
                self.assertIs(data.get('enabled'), enabled)
                self.assertIs(data.get('send_enabled'), enabled and send_enabled)
                self.assertNotIn('bot_token', data)
                self.binding.active = False
                self.binding.save(update_fields=['active'])
                unbound = self.api.get(self.url).json()
                self.assertIs(unbound.get('enabled'), enabled)
                self.assertIs(unbound.get('send_enabled'), enabled and send_enabled)
                self.binding.active = True
                self.binding.save(update_fields=['active'])
