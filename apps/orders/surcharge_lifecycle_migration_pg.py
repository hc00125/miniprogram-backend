"""Upgrade rehearsal from release schema, isolated private PostgreSQL ONLY."""
from decimal import Decimal
from django.test import TransactionTestCase
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


class ReleaseUpgradeTests(TransactionTestCase):
    def test_gifts0007_no_attempt_upgrade_preserves_old_income_withdrawal_and_wallet(self):
        self.assertEqual(connection.vendor, 'postgresql')
        db = connection.settings_dict
        self.assertEqual(db['NAME'], 'test_touchi_backend_v2')
        self.assertEqual(db['USER'], 'touchi_backend_v2')
        self.assertEqual(db['PORT'], '55459')
        self.assertIn('/backend-pg', db['HOST'])
        executor = MigrationExecutor(connection)
        final_targets = executor.loader.graph.leaf_nodes()
        targets = [('gifts', '0007_remove_gift_gift_positive_price_and_more'),
            ('orders', '0016_remove_room_entry_deadlines'), ('wallet', '0009_rechargeorder_checkout_order_no'),
            ('earnings', '0006_withdrawal_payout_batches')]
        try:
            executor.migrate(targets)
            old = executor.loader.project_state(targets).apps
            Purchase = old.get_model('gifts', 'GiftPurchase')
            self.assertNotIn('attempt', [f.name for f in Purchase._meta.fields])
            User = old.get_model('auth', 'User')
            user = User.objects.create(username='release-fixture')
            kind = old.get_model('catalog', 'PlayerType').objects.create(name='release', priority=1)
            player = old.get_model('players', 'Player').objects.create(name='release', player_type_id=kind.pk)
            package = old.get_model('catalog', 'Package').objects.create(name='release', base_price=10)
            order = old.get_model('orders', 'Order').objects.create(order_no='release-paid', boss_user_id=user.pk,
                package_id=package.pk, required_players=1, total_amount=10, paid=True, status='已完成')
            wallet = old.get_model('earnings', 'PlayerWallet').objects.create(player_id=player.pk,
                available_balance=Decimal('2'), withdrawing_balance=Decimal('3'))
            earning = old.get_model('earnings', 'PlayerEarning').objects.create(order_id=order.pk,
                player_id=player.pk, gross_amount=Decimal('10'), commission_rate=Decimal('20'),
                commission_amount=Decimal('2'), net_amount=Decimal('8'), review_until=timezone.now(),
                status='available', available_amount=Decimal('2'), withdrawing_amount=Decimal('3'), withdrawn_amount=Decimal('3'))
            withdrawal = old.get_model('earnings', 'Withdrawal').objects.create(withdrawal_no='release-withdraw',
                player_id=player.pk, wallet_id=wallet.pk, amount=Decimal('3'), account_name='offline', account_no='offline')
            allocation = old.get_model('earnings', 'WithdrawalAllocation').objects.create(withdrawal_id=withdrawal.pk,
                earning_id=earning.pk, amount=Decimal('3'))
            payment = old.get_model('payments', 'Payment').objects.create(payment_no='release-pay', order_id=order.order_no,
                channel='balance', scene='balance', amount=Decimal('10'), status='paid')
            saved = (earning.pk, wallet.pk, allocation.pk, payment.pk)
            executor = MigrationExecutor(connection)
            executor.migrate(final_targets)
            from apps.earnings.models import PlayerEarning, PlayerWallet, WithdrawalAllocation
            from apps.payments.models import Payment
            restored = PlayerEarning.objects.get(pk=saved[0])
            self.assertEqual(restored.source, 'order'); self.assertIsNone(restored.surcharge_id)
            self.assertEqual((restored.gross_amount, restored.net_amount, restored.available_amount,
                restored.withdrawing_amount, restored.withdrawn_amount), tuple(map(Decimal, ('10','8','2','3','3'))))
            self.assertEqual(PlayerWallet.objects.get(pk=saved[1]).available_balance, Decimal('2'))
            self.assertEqual(WithdrawalAllocation.objects.get(pk=saved[2]).earning_id, restored.pk)
            self.assertEqual(Payment.objects.get(pk=saved[3]).amount, Decimal('10'))
        finally:
            MigrationExecutor(connection).migrate(final_targets)
