"""Closed-history compatibility and active-intent safety, disposable PG only."""
from decimal import Decimal
from django.db import connection, transaction, DatabaseError
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from rest_framework.exceptions import ValidationError


class LegacyReleaseUpgradeTests(TransactionTestCase):
    def test_upgrade_skips_closed_history_and_protects_active_legacy(self):
        self.assertEqual(connection.vendor, 'postgresql')
        db = connection.settings_dict
        self.assertEqual((db['NAME'], db['USER'], db['PORT']),
            ('test_touchi_backend_v2', 'touchi_backend_v2', '55459'))
        self.assertEqual(db['HOST'], '/root/.hermes/cache/scratch/touchi-commerce-multiagent-20260923/backend-pg')
        executor = MigrationExecutor(connection)
        final = executor.loader.graph.leaf_nodes()
        targets = [('gifts', '0007_remove_gift_gift_positive_price_and_more'),
            ('orders', '0016_remove_room_entry_deadlines'), ('wallet', '0009_rechargeorder_checkout_order_no'),
            ('earnings', '0006_withdrawal_payout_batches')]
        try:
            executor.migrate(targets)
            # Unrelated apps remain at their actual latest schema. Include
            # accounts' leaf when rendering the historical fixture state.
            account_targets = [node for node in final if node[0] == 'accounts']
            old = executor.loader.project_state(targets + account_targets).apps
            user = old.get_model('auth', 'User').objects.create(username='legacy-release')
            profile = old.get_model('accounts', 'ClientProfile').objects.create(user_id=user.pk, openid='offline-legacy-release')
            wallet = old.get_model('wallet', 'ClientWallet').objects.create(profile_id=profile.pk, balance=Decimal('10'))
            package = old.get_model('catalog', 'Package').objects.create(name='legacy', base_price=6)
            order = old.get_model('orders', 'Order').objects.create(order_no='OLD-CANCELLED',
                boss_user_id=user.pk, package_id=package.pk, required_players=1, total_amount=6,
                paid=False, status='已取消')
            old.get_model('orders', 'OrderStatusLog').objects.create(order_id=order.pk,
                from_status='待接单', to_status='待支付', reason='synthetic old log')
            active = old.get_model('orders', 'Order').objects.create(order_no='OLD-ACTIVE',
                boss_user_id=user.pk, package_id=package.pk, required_players=1, total_amount=6,
                paid=False, status='待支付')
            old.get_model('wallet', 'RechargeOrder').objects.create(recharge_no='OLD-COIN-SOURCE',
                profile_id=profile.pk, amount=10, status='credited',
                notify_payload={'mode': 'short_series_coin', 'wechat_coin_units_per_yuan': 10})
            old.get_model('wallet', 'ClientWalletLedger').objects.create(wallet_id=wallet.pk,
                entry_type='recharge', amount=10, balance_after=10, reference_id='OLD-COIN-SOURCE')
            before = list(old.get_model('wallet', 'ClientWalletLedger').objects.values())
            executor = MigrationExecutor(connection); executor.migrate(final)
            from apps.wallet.spend_models import OrderWalletSpend, WalletSpendAttempt
            from apps.wallet.models import ClientWallet, ClientWalletLedger
            from apps.orders.models import Order
            from apps.wallet.coin_balance_service import pay_order_with_coin_aware_balance
            from django.contrib.auth.models import User
            from unittest.mock import patch
            self.assertFalse(OrderWalletSpend.objects.filter(order_id=order.pk).exists())
            self.assertEqual(Order.objects.get(pk=order.pk).status, '已取消')
            binding = OrderWalletSpend.objects.get(order_id=active.pk)
            self.assertEqual(binding.blocker, 'LEGACY_COIN_REVIEW_REQUIRED')
            self.assertIsNone(binding.attempt_id)
            self.assertEqual(binding.intent['prior_status'], '待支付')
            self.assertEqual(binding.intent['coin_recharge_nos'], ['OLD-COIN-SOURCE'])
            frozen = list(OrderWalletSpend.objects.filter(pk=binding.pk).values())
            mutations = (
                (lambda: OrderWalletSpend.objects.filter(pk=binding.pk).update(blocker=''), 'ORDER_SPEND_IDENTITY_IMMUTABLE'),
                (lambda: OrderWalletSpend.objects.filter(pk=binding.pk).delete(), 'ORDER_SPEND_AUDIT_IMMUTABLE'),
                (lambda: Order.objects.filter(pk=active.pk).update(status='已取消'), 'LEGACY_COIN_REVIEW_REQUIRED'),
            )
            for mutate, error in mutations:
                with self.subTest(error=error), self.assertRaises(DatabaseError) as caught:
                    with transaction.atomic(): mutate()
                self.assertEqual(caught.exception.__cause__.pgcode, '23514')
                self.assertIn(error, str(caught.exception))
            new = Order.objects.create(order_no='AFTER-UPGRADE', boss_user_id=user.pk,
                package_id=package.pk, required_players=1, total_amount=6, status=Order.STATUS_PENDING_PAYMENT)
            with patch('apps.wallet.spend_adapter.WeChatSpendAdapter') as adapter:
                with self.assertRaises(ValidationError) as caught:
                    pay_order_with_coin_aware_balance(new.order_no, User.objects.get(pk=user.pk), code='offline')
                self.assertEqual(str(caught.exception.detail['code']), 'LEGACY_COIN_REVIEW_REQUIRED')
                adapter.assert_not_called()
            self.assertEqual(frozen, list(OrderWalletSpend.objects.filter(pk=binding.pk).values()))
            self.assertEqual(before, list(ClientWalletLedger.objects.values()))
            self.assertEqual(ClientWallet.objects.get(pk=wallet.pk).balance, Decimal('10'))
            self.assertFalse(WalletSpendAttempt.objects.exists())
        finally:
            MigrationExecutor(connection).migrate(final)
