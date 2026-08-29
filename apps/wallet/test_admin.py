from decimal import Decimal

from django.contrib.auth.models import Permission, User
from django.test import Client, TestCase

from apps.accounts.models import ClientProfile

from .models import ClientWallet, ClientWalletLedger


class WalletAdminPermissionTests(TestCase):
    """B4 修复回归：手工调整余额动作必须要求 change 权限，只读人员不可执行。"""

    def setUp(self):
        owner = User.objects.create_user(username='wallet-admin-owner')
        profile = ClientProfile.objects.create(
            user=owner,
            openid='openid_wallet_admin',
            nickname='后台调整测试老板',
        )
        self.wallet = ClientWallet.objects.create(profile=profile, balance=Decimal('10.00'))
        self.changelist_url = '/admin/wallet/clientwallet/'

    def _staff(self, username, *codenames):
        user = User.objects.create_user(username=username, password='pw', is_staff=True)
        for codename in codenames:
            user.user_permissions.add(Permission.objects.get(codename=codename))
        return user

    def _post_adjust(self, user):
        client = Client()
        client.force_login(user)
        return client.post(self.changelist_url, {
            'action': 'manual_adjust',
            '_selected_action': [str(self.wallet.pk)],
            'amount': '999.00',
            'reason': '权限测试调整',
        })

    def test_view_only_staff_cannot_run_manual_adjust(self):
        viewer = self._staff('cs-viewer', 'view_clientwallet')
        self._post_adjust(viewer)
        self.assertFalse(ClientWalletLedger.objects.exists())
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('10.00'))

    def test_change_staff_can_run_manual_adjust(self):
        editor = self._staff('finance-editor', 'view_clientwallet', 'change_clientwallet')
        self._post_adjust(editor)
        self.assertEqual(ClientWalletLedger.objects.count(), 1)
        entry = ClientWalletLedger.objects.get()
        self.assertEqual(entry.entry_type, ClientWalletLedger.TYPE_ADMIN_ADJUST)
        self.assertEqual(entry.amount, Decimal('999.00'))
        self.assertEqual(entry.operator.username, 'finance-editor')
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('1009.00'))
