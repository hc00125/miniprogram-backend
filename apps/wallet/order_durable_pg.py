"""Only run on the task's own Unix-socket PG cluster; SQL errors are failures."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth.models import User
from django.db import connection, connections, transaction, DatabaseError
from django.test import TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError
from apps.orders.models import Order
from apps.wallet import test_order_durable as fixtures
from apps.wallet.models import WalletSpendAttempt, ClientWalletLedger
from apps.wallet.spend_models import OrderWalletSpend
from apps.wallet import order_spend, spend_service


@override_settings(WECHAT_VIRTUALPAY_ENV=1, SHARED_SPEND_PLATFORM_APPROVED=True, WECHAT_VIRTUALPAY_ENABLED=True,
    WECHAT_VIRTUALPAY_COIN_UNITS_PER_YUAN=10, ORDER_SURCHARGE_ENABLED=True,
    GIFT_PURCHASE_ENABLED=True, GIFT_TRANSACTION_POLICY={'version': 'pg-offline', 'platform_approved': True,
        'max_quantity': 10, 'max_diamonds': 1000, 'daily_diamonds': 2000, 'recipient_ids': []})
class DurableOrderPGTests(TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, 'postgresql')
        fixtures.OriginalOrderDurableTests.setUp(self)

    def race(self, *fns):
        barrier = Barrier(len(fns))
        def run(fn):
            connections.close_all()
            try:
                with connection.cursor() as cur:
                    cur.execute("SET lock_timeout='3s'; SET statement_timeout='8s'")
                barrier.wait(10)
                try:
                    return fn()
                except ValidationError as e:
                    return str(e.detail.get('code', 'validation')) if isinstance(e.detail, dict) else 'validation'
                # Never catch DatabaseError: 40P01/55P03/57014 must fail tests.
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=len(fns)) as pool:
            futures = [pool.submit(run, f) for f in fns]
            return [f.result(15) for f in futures]

    def adapter(self, timeout=False):
        case = self
        class Adapter:
            calls = []
            mutex = Lock()
            def authenticate(self, a, code): return 'ephemeral'
            def spend(self, a, session):
                case.assertFalse(connection.in_atomic_block)
                # A second actual connection sees dispatch and fixed request.
                probe = connections['default'].copy(alias='probe')
                try:
                    with probe.cursor() as cur:
                        cur.execute('SELECT status, request_payload, source_snapshot FROM wallet_walletspendattempt WHERE id=%s', [a.pk])
                        state, payload, source = cur.fetchone()
                    import json
                    if isinstance(payload, str): payload = json.loads(payload)
                    if isinstance(source, str): source = json.loads(source)
                    case.assertEqual((state, payload, source), ('dispatching', a.request_payload, a.source_snapshot))
                finally: probe.close()
                with self.mutex: self.calls.append(dict(a.request_payload))
                if timeout: raise TimeoutError()
                return {'errcode': 0, 'order_id': a.external_id}
            def query(self, a): return None
        return Adapter()

    def test_same_original_different_keys_remote_once_and_committed_payload(self):
        from .coin_balance_service import pay_order_with_coin_aware_balance
        adapter = self.adapter()
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter):
            result = self.race(*[lambda k=k: pay_order_with_coin_aware_balance(self.order.order_no,
                User.objects.get(pk=self.user.pk), code='fake', idempotency_key=k)['status'] for k in ('one', 'two')])
        self.assertEqual(len(adapter.calls), 1, result)
        self.assertEqual(WalletSpendAttempt.objects.count(), 1)
        self.assertEqual(Payment_count(self.order), 1)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('4'))

    def test_order_gift_checkout_three_way_coin_race_no_overdraft(self):
        from apps.gifts.models import Gift
        from apps.gifts.services import purchases
        from apps.orders.surcharge_models import OrderCheckout, OrderSurcharge
        from apps.orders.surcharges import quote_amount
        from apps.orders.checkout_payment import pay as checkout_pay
        from .coin_balance_service import pay_order_with_coin_aware_balance
        gift = Gift.objects.create(name='race gift', price_diamonds=60)
        Gift.objects.filter(pk=gift.pk).update(is_active=True)
        q = purchases.quote(self.user, gift_code=gift.code, quantity=1, mode='inventory')
        other = Order.objects.create(order_no='PG-CHECKOUT', boss_user=self.user, package=self.package,
            required_players=1, total_price_per_hour=5, total_amount=5, status=Order.STATUS_PENDING_PAYMENT,
            surcharge_guarded=True)
        sc = OrderSurcharge.objects.create(order=other, boss=self.user, surcharge_no='PG-SC', amount_diamonds=10,
            amount_yuan=1, idempotency_key='pg-sc', request_digest='a'*64, allocation_snapshot=quote_amount(10, 1))
        OrderCheckout.objects.create(order=other, boss=self.user, surcharge=sc, idempotency_key='pg-checkout',
            request_digest='b'*64, quote_snapshot={'quote_version':'pg', 'surcharge': sc.allocation_snapshot},
            base_amount=5, surcharge_amount=1, total_amount=6)
        adapter = self.adapter()
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter):
            result = self.race(
                lambda: pay_order_with_coin_aware_balance(self.order.order_no, User.objects.get(pk=self.user.pk), code='fake')['status'],
                lambda: purchases.purchase(User.objects.get(pk=self.user.pk), gift_code=gift.code, quantity=1,
                    mode='inventory', price_version=q['price_version'], idempotency_key='pg-gift', code='fake').status,
                lambda: checkout_pay(other.order_no, User.objects.get(pk=self.user.pk), code='fake')['status'])
        self.assertEqual(result.count('paid'), 1, result)
        self.assertTrue(all(x in ('paid', 'PAYMENT_PENDING', 'INSUFFICIENT_BALANCE') for x in result), result)
        self.assertEqual(len(adapter.calls), 1)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('4'))
        self.assertEqual(ClientWalletLedger.objects.filter(entry_type='shared_spend').count(), 1)

    def test_fresh_process_recovery_twice_after_remote_success_local_failure(self):
        import os, subprocess, sys
        from apps.wallet.coin_balance_service import pay_order_with_coin_aware_balance
        adapter = self.adapter()
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter), \
             patch('apps.wallet.order_spend.fulfill', side_effect=RuntimeError('injected local crash')):
            with self.assertRaises(RuntimeError):
                pay_order_with_coin_aware_balance(self.order.order_no, self.user, code='fake')
        a = WalletSpendAttempt.objects.get(kind='order')
        self.assertEqual(a.status, 'succeeded')
        self.assertEqual(Payment_count(self.order), 0)
        code = """
import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings_surcharge_v2_pg_test'
from django.conf import settings
assert settings.DATABASES['default']['USER'] == 'touchi_backend_v2'
assert settings.DATABASES['default']['HOST'] == '/root/.hermes/cache/scratch/touchi-commerce-multiagent-20260923/backend-pg'
assert settings.DATABASES['default']['PORT'] == '55459'
settings.DATABASES['default']['NAME'] = 'test_touchi_backend_v2'
import django
django.setup()
from django.core.management import call_command
import sys
print('RECOVERY_CHILD_PID', os.getpid())
call_command('reconcile_wallet_spends', apply=True, attempt_id=int(sys.argv[1]))
"""
        for _ in range(2):
            child = subprocess.run([sys.executable, '-c', code, str(a.pk)],
                cwd=str(__import__('django.conf', fromlist=['settings']).settings.BASE_DIR),
                capture_output=True, text=True, timeout=40)
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            print('RECOVERY_PARENT_PID', os.getpid(), child.stdout.strip())
        a.refresh_from_db(); self.wallet.refresh_from_db()
        self.assertEqual(a.status, 'completed')
        self.assertEqual(self.wallet.balance, Decimal('4'))
        self.assertEqual(Payment_count(self.order), 1)
        self.assertEqual(ClientWalletLedger.objects.filter(reference_id=a.external_id).count(), 1)
        self.assertEqual(len(adapter.calls), 1)

    def test_unknown_sql_cancel_and_identity_update_exact_23514(self):
        a = order_spend.prepare(self.order.order_no, self.user)
        spend_service.execute(a.pk, adapter=self.adapter(timeout=True), apply=order_spend.fulfill)
        for changes in ({'status': Order.STATUS_CANCELLED}, {'total_amount': 9}):
            with self.assertRaises(DatabaseError) as caught:
                with transaction.atomic(): Order.objects.filter(pk=self.order.pk).update(**changes)
            self.assertEqual(caught.exception.__cause__.pgcode, '23514')
            self.assertIn('ORDER_PAYMENT_PENDING', str(caught.exception))
        with self.assertRaises(DatabaseError) as caught:
            with transaction.atomic(): OrderWalletSpend.objects.filter(order=self.order).update(intent={})
        self.assertEqual(caught.exception.__cause__.pgcode, '23514')

    def test_cancel_races_prepared_dispatch_never_cancels_captured_order(self):
        from apps.orders.services import cancel_order
        a = order_spend.prepare(self.order.order_no, self.user)
        adapter = self.adapter()
        results = self.race(lambda: spend_service.execute(a.pk, adapter=adapter, apply=order_spend.fulfill).status,
            lambda: cancel_order(Order.objects.get(pk=self.order.pk)).status)
        a.refresh_from_db(); self.order.refresh_from_db(); self.wallet.refresh_from_db()
        if a.status == 'completed':
            self.assertTrue(self.order.paid)
            self.assertNotEqual(self.order.status, Order.STATUS_CANCELLED)
            self.assertEqual(len(adapter.calls), 1)
        else:
            self.assertEqual((a.status, self.order.status, adapter.calls), ('failed', Order.STATUS_CANCELLED, []), results)
            self.assertEqual(self.wallet.balance, Decimal('10'))


def Payment_count(order):
    from apps.payments.models import Payment
    return Payment.objects.filter(order=order).count()
