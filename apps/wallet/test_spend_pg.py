"""PostgreSQL concurrency evidence with independent DB connections."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from decimal import Decimal
from django.db import connection, connections, transaction, DatabaseError
from django.test import TransactionTestCase, override_settings
from django.contrib.auth.models import User
from rest_framework.exceptions import ValidationError
from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order
from .models import ClientWallet, WalletSpendAttempt, ClientWalletLedger, RechargeOrder
from . import spend_service as spend


@override_settings(WECHAT_VIRTUALPAY_ENV=1)
class SpendConcurrencyPGTests(TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, 'postgresql', 'Run with isolated PG settings')
        self.user = User.objects.create_user('shared-pg')
        self.profile = ClientProfile.objects.create(user=self.user, openid='shared-pg')
        self.wallet, _ = ClientWallet.objects.update_or_create(profile=self.profile, defaults={'balance': Decimal('10.00')})

    def race(self, *functions):
        barrier = Barrier(len(functions))
        def run(fn):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                return fn()
            except ValidationError:
                return 'blocked'
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            return list(pool.map(run, functions))

    def reserve(self, key):
        return spend.reserve(ClientProfile.objects.get(pk=self.profile.pk), kind='gift', business_no=key,
            key=key, amount=Decimal('6.00'), intent={})

    def test_two_reservations_compete_for_balance_and_coin_sources(self):
        results = self.race(lambda: self.reserve('a').status, lambda: self.reserve('b').status)
        self.assertCountEqual(results, ['prepared', 'blocked'])
        self.assertEqual(WalletSpendAttempt.objects.count(), 1)

    def test_gift_and_original_order_compete_without_overdraft(self):
        from .coin_balance_service import pay_order_with_coin_aware_balance
        order = Order.objects.create(order_no='PGORDER', boss_user=self.user,
            package=Package.objects.create(name='pg', base_price=6), required_players=1,
            total_price_per_hour=6, total_amount=6, status=Order.STATUS_PENDING_PAYMENT)
        result = self.race(lambda: self.reserve('gift').status,
            lambda: pay_order_with_coin_aware_balance(order.order_no, User.objects.get(pk=self.user.pk))['status'])
        self.assertEqual(result.count('blocked'), 1)
        self.wallet.refresh_from_db()
        self.assertGreaterEqual(self.wallet.balance - spend.reserved(self.wallet), Decimal('0'))

    def test_concurrent_same_attempt_finalization_is_exactly_once(self):
        attempt = self.reserve('once')
        results = self.race(lambda: spend.execute(attempt.pk).status, lambda: spend.execute(attempt.pk).status)
        spend.recover(attempt.pk)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('4.00'))
        self.assertEqual(self.wallet.spent_total, Decimal('6.00'))
        self.assertEqual(ClientWalletLedger.objects.filter(reference_id=attempt.external_id).count(), 1)

    def test_immutable_request_cannot_be_changed_by_bulk_sql(self):
        attempt = self.reserve('immutable')
        with self.assertRaises(DatabaseError), transaction.atomic():
            WalletSpendAttempt.objects.filter(pk=attempt.pk).update(external_id='MUTATED')
        with self.assertRaises(DatabaseError), transaction.atomic():
            WalletSpendAttempt.objects.filter(pk=attempt.pk).update(source_snapshot={})
