from decimal import Decimal
from importlib.util import find_spec
from django.contrib.auth.models import User
from django.test import TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError
from apps.accounts.models import ClientProfile
from .models import ClientWallet, ClientWalletLedger


@override_settings(WECHAT_VIRTUALPAY_ENV=1)
class SharedSpendTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='shared-buyer')
        self.profile = ClientProfile.objects.create(user=self.user, openid='synthetic-shared')
        self.wallet, _ = ClientWallet.objects.update_or_create(profile=self.profile, defaults={'balance': Decimal('10.00')})

    def engine(self):
        self.assertIsNotNone(find_spec('apps.wallet.spend_service'), 'Persistent shared spend engine missing')
        from . import spend_service
        return spend_service

    def test_shared_spend_has_explicit_ledger_category(self):
        self.assertIn('shared_spend', dict(ClientWalletLedger.ENTRY_TYPE_CHOICES))

    def test_checkout_reserved_coin_cannot_be_reassigned_even_with_free_manual_value(self):
        from apps.catalog.models import Package
        from apps.orders.models import Order
        from .models import RechargeOrder
        order = Order.objects.create(order_no='checkout-hold', boss_user=self.user,
            package=Package.objects.create(name='hold', base_price=3), total_amount=3, required_players=1,
            status=Order.STATUS_PENDING_PAYMENT)
        RechargeOrder.objects.create(recharge_no='CHECKOUT-HOLD', profile=self.profile, amount=Decimal('3.00'),
            checkout_order_no=order.order_no, status='credited', notify_payload={'mode': 'short_series_coin'})
        ClientWalletLedger.objects.create(wallet=self.wallet, entry_type='recharge', amount=Decimal('3.00'),
            balance_after=Decimal('3.00'), reference_id='CHECKOUT-HOLD')
        with self.assertRaises(ValidationError):
            self.engine().reserve(self.profile, kind='gift', business_no='compete', key='compete', amount=Decimal('1.00'), intent={})

    def test_source_snapshot_has_exact_recharge_provenance(self):
        attempt = self.coin_attempt()
        self.assertIn('allocations', attempt.source_snapshot)
        self.assertEqual(attempt.source_snapshot['allocations'][0]['reference_id'], 'SYNTHETIC-COIN')
        self.assertEqual(attempt.source_snapshot['allocations'][0]['amount'], '5.00')
        self.assertEqual(attempt.source_snapshot['local_amount'], '1.00')

    def test_spend_admin_is_auditable_read_only_even_for_superuser(self):
        from django.contrib import admin
        from django.test import RequestFactory
        from .models import WalletSpendAttempt, WalletSpendAudit
        self.assertIn(WalletSpendAttempt, admin.site._registry)
        self.assertIn(WalletSpendAudit, admin.site._registry)
        actor = User.objects.create_superuser('audit-admin', password='synthetic')
        request = RequestFactory().get('/admin/')
        request.user = actor
        for model in (WalletSpendAttempt, WalletSpendAudit):
            view = admin.site._registry[model]
            self.assertFalse(view.has_add_permission(request))
            self.assertFalse(view.has_change_permission(request))
            self.assertFalse(view.has_delete_permission(request))

    def test_ios_net_credit_does_not_claim_historical_manual_balance(self):
        from .models import RechargeOrder
        from .coin_sync import coin_backed_wallet_buckets
        ClientWalletLedger.objects.create(wallet=self.wallet, entry_type='admin_adjust', amount=Decimal('3.00'), balance_after=Decimal('3.00'))
        RechargeOrder.objects.create(recharge_no='IOS-NET', profile=self.profile, amount=Decimal('10.00'),
            status='credited', notify_payload={'mode': 'short_series_coin', 'wechat_coin_units_per_yuan': 10, 'credited_amount_yuan': '7.00'})
        ClientWalletLedger.objects.create(wallet=self.wallet, entry_type='recharge', amount=Decimal('7.00'),
            balance_after=Decimal('10.00'), reference_id='IOS-NET')
        self.assertEqual(coin_backed_wallet_buckets(self.profile), {10: Decimal('7.00')})

    def test_shared_source_replay_is_explicit_not_generic_negative_heuristic(self):
        engine = self.engine()
        attempt = self.coin_attempt()
        ClientWalletLedger.objects.create(wallet=self.wallet, entry_type='admin_adjust', amount=Decimal('5.00'), balance_after=Decimal('10.00'))
        class Adapter:
            def authenticate(self, attempt, code): return 'ephemeral'
            def spend(self, attempt, session): return {'errcode': 0, 'order_id': attempt.external_id}
        engine.execute(attempt.pk, adapter=Adapter())
        from .coin_sync import coin_backed_wallet_buckets
        self.assertEqual(coin_backed_wallet_buckets(self.profile), {})

    def coin_attempt(self):
        from .models import RechargeOrder
        RechargeOrder.objects.create(recharge_no='SYNTHETIC-COIN', profile=self.profile, amount=Decimal('5.00'),
            status='credited', notify_payload={'mode': 'short_series_coin', 'wechat_coin_units_per_yuan': 10})
        ClientWalletLedger.objects.create(wallet=self.wallet, entry_type='recharge', amount=Decimal('5.00'),
            balance_after=Decimal('5.00'), reference_id='SYNTHETIC-COIN')
        return self.engine().reserve(self.profile, kind='gift', business_no='GC', key='coin', amount=Decimal('6.00'), intent={})

    def test_unknown_recovery_queries_only_and_keeps_reservation(self):
        engine = self.engine()
        attempt = self.coin_attempt()
        class Adapter:
            spends = 0
            queries = 0
            def authenticate(self, attempt, code): return 'ephemeral'
            def spend(self, attempt, session):
                self.spends += 1
                raise TimeoutError()
            def query(self, attempt):
                self.queries += 1
                return None
        adapter = Adapter()
        self.assertEqual(engine.execute(attempt.pk, adapter=adapter).status, 'unknown')
        self.assertTrue(hasattr(engine, 'recover'), 'query-only recovery missing')
        for _ in range(2):
            engine.recover(attempt.pk, adapter=adapter)
            engine.execute(attempt.pk, adapter=adapter)
        self.assertEqual((adapter.spends, adapter.queries), (1, 2))
        attempt.refresh_from_db()
        self.assertEqual(attempt.reserved_amount, Decimal('6.00'))
        self.assertEqual(attempt.blocker, 'OFFICIAL_QUERY_UNAVAILABLE')

    def test_remote_success_local_failure_recovers_without_second_spend(self):
        engine = self.engine()
        attempt = self.coin_attempt()
        class Adapter:
            spends = 0
            def authenticate(self, attempt, code): return 'ephemeral'
            def spend(self, attempt, session):
                self.spends += 1
                return {'errcode': 0, 'order_id': attempt.external_id}
        adapter = Adapter()
        def broken(attempt): raise RuntimeError('simulated local failure')
        with self.assertRaises(RuntimeError):
            engine.execute(attempt.pk, adapter=adapter, apply=broken)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'succeeded')
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('10.00'))
        self.assertTrue(hasattr(engine, 'recover'), 'query-only recovery missing')
        engine.recover(attempt.pk, adapter=adapter)
        engine.recover(attempt.pk, adapter=adapter)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('4.00'))
        self.assertEqual(adapter.spends, 1)

    def test_all_ledger_debits_and_original_order_respect_reservation(self):
        from .services import write_wallet_ledger
        from django.db import transaction
        engine = self.engine()
        engine.reserve(self.profile, kind='gift', business_no='G2', key='hold', amount=Decimal('6.00'), intent={})
        with self.assertRaises(ValidationError):
            with transaction.atomic():
                write_wallet_ledger(self.wallet, 'admin_adjust', Decimal('-5.00'), reference_id='admin')
        from apps.catalog.models import Package
        from apps.orders.models import Order
        from .coin_balance_service import pay_order_with_coin_aware_balance
        package = Package.objects.create(name='Synthetic', player_count=1, base_price=5)
        order = Order.objects.create(order_no='SHARED_ORDER', boss_user=self.user, package=package,
            required_players=1, total_price_per_hour=5, total_amount=5, status=Order.STATUS_PENDING_PAYMENT)
        with self.assertRaises(ValidationError):
            pay_order_with_coin_aware_balance(order.order_no, self.user)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('10.00'))

    def test_reserve_finalize_and_idempotency_are_real_ledger_operations(self):
        engine = self.engine()
        attempt = engine.reserve(self.profile, kind='gift', business_no='G1', key='key-1',
                                 amount=Decimal('6.00'), intent={'quantity': 2})
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('10.00'))
        self.assertEqual(attempt.reserved_amount, Decimal('6.00'))
        same = engine.reserve(self.profile, kind='gift', business_no='G1', key='key-1',
                              amount=Decimal('6.00'), intent={'quantity': 2})
        self.assertEqual(same.pk, attempt.pk)
        with self.assertRaises(ValidationError):
            engine.reserve(self.profile, kind='gift', business_no='G1', key='key-1',
                           amount=Decimal('5.00'), intent={'quantity': 2})
        engine.execute(attempt.pk)
        engine.execute(attempt.pk)
        self.wallet.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'completed')
        self.assertEqual(self.wallet.balance, Decimal('4.00'))
        self.assertEqual(self.wallet.spent_total, Decimal('6.00'))
        self.assertEqual(ClientWalletLedger.objects.filter(reference_id=attempt.external_id).count(), 1)
