from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from apps.wallet.models import ClientWallet, ClientWalletLedger

from .client_wallet_admin_patch import adjust_client_wallets
from .models import ClientProfile


class ClientProfileWalletAdjustmentAdminTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_user(
            username='wallet-admin',
            password='test-password',
            is_staff=True,
        )
        self.client_user = User.objects.create_user(username='new-client')
        self.profile = ClientProfile.objects.create(
            user=self.client_user,
            openid='openid-new-client',
            nickname='未消费老板',
        )
        self.request_factory = RequestFactory()

    def test_adjustment_creates_wallet_for_unconsumed_user_and_writes_ledger(self):
        self.assertFalse(ClientWallet.objects.filter(profile=self.profile).exists())

        request = self.request_factory.post('/admin/accounts/clientprofile/', {
            'wallet_diamonds': '500',
            'wallet_adjust_reason': '线下充值补录',
        })
        request.user = self.admin_user
        model_admin = SimpleNamespace(message_user=Mock())

        adjust_client_wallets(
            model_admin,
            request,
            ClientProfile.objects.filter(pk=self.profile.pk),
        )

        wallet = ClientWallet.objects.get(profile=self.profile)
        self.assertEqual(wallet.balance, Decimal('50.00'))

        ledger = ClientWalletLedger.objects.get(wallet=wallet)
        self.assertEqual(ledger.entry_type, ClientWalletLedger.TYPE_ADMIN_ADJUST)
        self.assertEqual(ledger.amount, Decimal('50.00'))
        self.assertEqual(ledger.balance_after, Decimal('50.00'))
        self.assertEqual(ledger.operator, self.admin_user)
        self.assertIn('线下充值补录', ledger.note)
        self.assertIn('+500', ledger.note)

        model_admin.message_user.assert_called()
