from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order

from .coin_balance_service import pay_order_with_coin_aware_balance
from .models import ClientWallet, ClientWalletLedger, RechargeOrder


VIRTUAL_SETTINGS = {
    'WECHAT_VIRTUALPAY_ENABLED': True,
    'WECHAT_APP_ID': 'wx_test_appid',
    'WECHAT_APP_SECRET': 'test_secret',
    'WECHAT_VIRTUALPAY_OFFER_ID': 'test_offer',
    'WECHAT_VIRTUALPAY_APP_KEY': 'test_app_key',
    'WECHAT_VIRTUALPAY_ENV': 0,
    'WECHAT_VIRTUALPAY_HTTP_TIMEOUT': 10,
    'WECHAT_VIRTUALPAY_COIN_UNITS_PER_YUAN': 10,
    'ENABLE_MOCK_PAYMENT': False,
}


@override_settings(**VIRTUAL_SETTINGS)
class CoinRemoteSuccessRetryTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='coin-idempotency-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='openid_coin_idempotency',
            nickname='Coin幂等恢复老板',
        )
        self.package = Package.objects.create(name='Coin幂等恢复商品', player_count=1, base_price=20)
        self.order = Order.objects.create(
            order_no='COINIDEMPOTENT001',
            boss_user=self.user,
            boss_wechat='coin-idempotency',
            package=self.package,
            required_players=1,
            total_price_per_hour=20,
            total_amount=20,
            status=Order.STATUS_PENDING_PAYMENT,
        )
        self.wallet, _ = ClientWallet.objects.update_or_create(profile=self.profile,
            defaults={'balance': Decimal('20.00'), 'recharged_total': Decimal('20.00')})
        recharge = RechargeOrder.objects.create(
            recharge_no='RCGCOINIDEMPOTENT1',
            profile=self.profile,
            product=None,
            amount=Decimal('20.00'),
            channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
            status=RechargeOrder.STATUS_CREDITED,
            notify_payload={
                'mode': 'short_series_coin',
                'wechat_coin_units_per_yuan': 10,
            },
        )
        ClientWalletLedger.objects.create(
            wallet=self.wallet,
            entry_type=ClientWalletLedger.TYPE_RECHARGE,
            amount=Decimal('20.00'),
            balance_after=Decimal('20.00'),
            reference_type='recharge_order',
            reference_id=recharge.recharge_no,
        )

    def test_legacy_uncertain_capture_is_quarantined_never_replayed(self):
        """Old low-balance/CP reconstruction is deliberately forbidden now."""
        from django.apps import apps
        from django.db import connection
        from importlib import import_module
        from .spend_models import OrderWalletSpend, WalletSpendAttempt
        migration = import_module('apps.wallet.migrations.0014_original_order_admission_guards')
        migration.quarantine_legacy(apps, connection.schema_editor())
        with patch('apps.wallet.coin_sync.user_xpay_post') as old_remote, \
             patch('apps.wallet.spend_adapter.user_xpay_post') as new_remote:
            with self.assertRaises(ValidationError) as caught:
                pay_order_with_coin_aware_balance(self.order.order_no, self.user, code='retry')
        self.assertEqual(str(caught.exception.detail['code']), 'LEGACY_COIN_REVIEW_REQUIRED')
        old_remote.assert_not_called(); new_remote.assert_not_called()
        self.assertFalse(WalletSpendAttempt.objects.exists())
        binding = OrderWalletSpend.objects.get(order=self.order)
        self.assertIsNone(binding.attempt_id)
        self.assertIn('RCGCOINIDEMPOTENT1', binding.intent['coin_recharge_nos'])
        self.order.refresh_from_db(); self.wallet.refresh_from_db()
        self.assertFalse(self.order.paid)
        self.assertEqual(self.wallet.balance, Decimal('20.00'))
        self.assertEqual(ClientWalletLedger.objects.count(), 1)
