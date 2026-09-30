from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from apps.catalog.models import PlayerType
from apps.players.models import Player

@override_settings(KOOK_ENABLED=True)
class BindingTests(TestCase):
    def setUp(self):
        self.player = Player.objects.create(name='offline-player', user=get_user_model().objects.create_user('offline'), player_type=PlayerType.objects.create(name='offline', priority=99))

    def test_five_failed_nonces_cancel_challenge(self):
        from apps.kook_integration import binding
        from apps.kook_integration.secrets import KookError
        c,code=binding.create_challenge(self.player,'bind')
        binding.claim_code(code,'fake-user','event')
        for _ in range(5):
            with self.assertRaises(KookError):
                binding.confirm(self.player,c.pk,'0'*64,True)
        c.refresh_from_db()
        self.assertEqual(c.attempts,5)
        self.assertEqual(c.state,'cancelled')

    def test_api_strict_jwt_and_full_flow(self):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken
        from apps.accounts.models import ClientProfile
        from apps.kook_integration import binding
        ClientProfile.objects.create(user=self.player.user, openid='offline-openid')
        api = APIClient()
        base = '/api/player/kook-binding'
        self.assertEqual(api.get(base).status_code,401)
        api.credentials(HTTP_AUTHORIZATION='Bearer not-a-jwt')
        self.assertEqual(api.get(base).status_code,401)
        api.credentials(HTTP_AUTHORIZATION='Bearer '+str(AccessToken.for_user(self.player.user)))
        self.assertEqual(api.post(base+'/challenges', {'purpose':'bind','player_id':123},format='json').status_code,400)
        response=api.post(base+'/challenges', {'purpose':'bind'},format='json')
        self.assertEqual(response.status_code,201, response.content)
        data=response.json(); url=base+'/challenges/'+data['challenge_id']
        binding.claim_code(data['code'],'offline-user','evt')
        nonce=api.get(url).json()['confirmation_nonce']
        response=api.post(url+'/confirm',{'confirmation_nonce':nonce,'notifications_enabled':True},format='json')
        self.assertEqual(response.status_code,200,response.content)
        version=response.json()['binding_version']
        self.assertEqual(api.delete(base,{'binding_version':'00000000-0000-0000-0000-000000000000'},format='json').status_code,409)
        self.assertEqual(api.delete(base,{'binding_version':version},format='json').status_code,204)
        self.assertEqual(api.get(base).json()['status'],'unbound')

    def test_private_proof_requires_final_confirmation(self):
        from apps.kook_integration import binding
        from apps.kook_integration.models import KookBinding
        challenge, code = binding.create_challenge(self.player, 'bind')
        self.assertEqual(len(code), 10)
        self.assertNotEqual(challenge.digest, code)
        binding.claim_code(code, 'fake-kook-user', 'event-1', 'nickname')
        self.assertFalse(KookBinding.objects.filter(active=True).exists())
        challenge.refresh_from_db()
        nonce = binding.confirmation_nonce(challenge)
        result = binding.confirm(self.player, challenge.pk, nonce, True)
        self.assertEqual(result.kook_user_id, 'fake-kook-user')
        self.assertEqual(binding.confirm(self.player, challenge.pk, nonce, True).pk, result.pk)
