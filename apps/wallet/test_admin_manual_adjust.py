from decimal import Decimal

from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase

from apps.accounts.models import ClientProfile

from .admin import ClientWalletAdmin, ManualWalletAdjustmentForm
from .models import ClientWallet, ClientWalletLedger


class ClientWalletManualAdjustmentAdminTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username='wallet-admin',
            password='test-password',
            email='wallet-admin@example.com',
        )
        self.client_user = User.objects.create_user(username='new-client')
        self.profile = ClientProfile.objects.create(
            user=self.client_user,
            openid='openid-new-client',
            nickname='未消费老板',
        )
        self.request_factory = RequestFactory()
        self.model_admin = ClientWalletAdmin(ClientWallet, admin.site)

    def attach_admin_state(self, request):
        middleware = SessionMiddleware(lambda _request: None)
        middleware.process_request(request)
        request.session.save()
        request._messages = FallbackStorage(request)
        request.user = self.admin_user
        return request

    def test_form_can_select_registered_user_without_wallet(self):
        self.assertFalse(ClientWallet.objects.filter(profile=self.profile).exists())

        form = ManualWalletAdjustmentForm(admin_site=admin.site)

        self.assertTrue(form.fields['profile'].queryset.filter(pk=self.profile.pk).exists())

    def test_manual_adjust_view_creates_wallet_and_ledger_for_unconsumed_user(self):
        self.assertFalse(ClientWallet.objects.filter(profile=self.profile).exists())
        request = self.attach_admin_state(
            self.request_factory.post(
                '/admin/wallet/clientwallet/manual-adjust/',
                {
                    'profile': str(self.profile.pk),
                    'diamonds': '500',
                    'reason': '线下充值补录',
                },
            )
        )

        response = self.model_admin.manual_adjust_view(request)

        self.assertEqual(response.status_code, 302)
        wallet = ClientWallet.objects.get(profile=self.profile)
        self.assertEqual(wallet.balance, Decimal('50.00'))

        ledger = ClientWalletLedger.objects.get(wallet=wallet)
        self.assertEqual(ledger.entry_type, ClientWalletLedger.TYPE_ADMIN_ADJUST)
        self.assertEqual(ledger.amount, Decimal('50.00'))
        self.assertEqual(ledger.balance_after, Decimal('50.00'))
        self.assertEqual(ledger.operator, self.admin_user)
        self.assertIn('线下充值补录', ledger.note)
        self.assertIn('+500', ledger.note)

    def test_custom_admin_url_is_registered(self):
        names = {url.name for url in self.model_admin.get_urls()}
        self.assertIn('wallet_clientwallet_manual_adjust', names)
