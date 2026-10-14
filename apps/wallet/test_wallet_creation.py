from django.contrib import admin
from django.contrib.auth.models import User
from django.test import Client, TransactionTestCase

from apps.accounts.models import ClientProfile

from .models import ClientWallet


class ClientWalletCreationTests(TransactionTestCase):
    reset_sequences = True

    def _create_profile(self, suffix):
        user = User.objects.create_user(username=f'wallet-profile-{suffix}')
        return ClientProfile.objects.create(
            user=user,
            openid=f'openid_wallet_profile_{suffix}',
            nickname=f'钱包客户{suffix}',
        )

    def test_new_client_profile_automatically_gets_empty_wallet(self):
        profile = self._create_profile('auto')

        wallet = ClientWallet.objects.get(profile=profile)
        self.assertEqual(wallet.balance, 0)
        self.assertEqual(wallet.recharged_total, 0)
        self.assertEqual(wallet.spent_total, 0)

    def test_admin_action_backfills_missing_wallet_without_duplicates(self):
        profile = self._create_profile('legacy')
        ClientWallet.objects.filter(profile=profile).delete()
        self.assertFalse(ClientWallet.objects.filter(profile=profile).exists())

        administrator = User.objects.create_superuser(
            username='wallet-backfill-admin',
            email='admin@example.com',
            password='pw',
        )
        client = Client()
        client.force_login(administrator)
        response = client.post('/admin/accounts/clientprofile/', {
            'action': 'ensure_client_wallets',
            '_selected_action': [str(profile.pk)],
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ClientWallet.objects.filter(profile=profile).count(), 1)

        response = client.post('/admin/accounts/clientprofile/', {
            'action': 'ensure_client_wallets',
            '_selected_action': [str(profile.pk)],
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ClientWallet.objects.filter(profile=profile).count(), 1)
