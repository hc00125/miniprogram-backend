"""Strict two-connection PostgreSQL tests, run only in task cluster."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from django.test import TransactionTestCase
from django.db import connection, connections, transaction
from django.contrib.auth.models import User
from apps.catalog.models import Package, PlayerType
from apps.orders.models import Order, OrderPlayer
from apps.players.models import Player


class LegacyLockOrderTests(TransactionTestCase):
    def test_old_lineup_update_does_not_wait_on_parent_trigger(self):
        self.assertEqual(connection.vendor, 'postgresql')
        user = User.objects.create_user('lock-v2')
        order = Order.objects.create(order_no='lock-v2', boss_user=user, required_players=1,
            package=Package.objects.create(name='legacy', base_price=1))
        player = Player.objects.create(name='legacy', player_type=PlayerType.objects.create(name='legacy', priority=1))
        member = OrderPlayer.objects.create(order=order, player=player)
        ready = Event(); done = Event()
        def bare_update():
            connections.close_all()
            try:
                ready.wait(5)
                with transaction.atomic():
                    with connections['default'].cursor() as cur:
                        cur.execute("SET LOCAL lock_timeout='1200ms'")
                    OrderPlayer.objects.filter(pk=member.pk).update(status='已完成')
                done.set()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Order.objects.select_for_update().get(pk=order.pk)
                future = pool.submit(bare_update); ready.set()
                # A real timeout/deadlock is a failure, never a successful guard.
                future.result(timeout=5)
                self.assertTrue(done.is_set())
                OrderPlayer.objects.select_for_update().get(pk=member.pk)
        member.refresh_from_db(); self.assertEqual(member.status, '已完成')

    def test_old_order_cannot_be_promoted_to_surcharge_after_visibility(self):
        from django.db import DatabaseError
        order = Order.objects.create(order_no='immutable-marker', required_players=1,
            package=Package.objects.create(name='legacy', base_price=1))
        with self.assertRaises(DatabaseError) as caught:
            with transaction.atomic():
                Order.objects.filter(pk=order.pk).update(surcharge_guarded=True)
        self.assertEqual(caught.exception.__cause__.pgcode, '23514')
        self.assertIn('SURCHARGE_MARKER_IMMUTABLE', str(caught.exception))


from django.test import override_settings
from apps.orders import test_surcharge_v2 as checkout_fixtures

@override_settings(ORDER_SURCHARGE_ENABLED=True, WECHAT_VIRTUALPAY_ENV=1)
class CheckoutRaceTests(TransactionTestCase):
    setUp = checkout_fixtures.CheckoutPaymentTests.setUp
    add_coin_source = checkout_fixtures.CheckoutPaymentTests.add_coin_source

    def race(self, fn):
        from threading import Barrier
        barrier = Barrier(2)
        def run(_):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                with connections['default'].cursor() as cur:
                    cur.execute("SET lock_timeout='4s'")
                return fn()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, i) for i in range(2)]
            return [f.result(timeout=15) for f in futures]

    def test_concurrent_confirmation_debits_once_without_deadlock(self):
        from apps.orders.checkout_payment import pay
        from apps.wallet.models import WalletSpendAttempt, ClientWalletLedger
        results = self.race(lambda: pay(self.order.order_no, User.objects.get(pk=self.user.pk)))
        self.wallet.refresh_from_db()
        self.assertEqual(str(self.wallet.balance), '18.80')
        self.assertEqual(WalletSpendAttempt.objects.count(), 1)
        self.assertEqual(WalletSpendAttempt.objects.get().status, 'completed')
        self.assertEqual(ClientWalletLedger.objects.filter(entry_type='shared_spend').count(), 1)
        self.assertTrue(any(x['status'] == 'paid' for x in results), results)

    def test_concurrent_creation_same_key_returns_one_new_parent(self):
        from apps.orders.checkout import quote_order
        from apps.orders.duration_services import create_order
        from apps.orders.serializers import OrderCreateSerializer
        serializer = OrderCreateSerializer(data={'boss_wechat': 'race', 'package_id': self.order.package_id,
            'surcharge_diamonds': 12})
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        data.update(quote_version=quote_order(data)['quote_version'], idempotency_key='concurrent-create')
        results = self.race(lambda: create_order(data, User.objects.get(pk=self.user.pk)).pk)
        self.assertEqual(results[0], results[1])
        self.assertEqual(Order.objects.count(), 2)

    def test_paid_guarded_lineup_fails_exact_business_constraint(self):
        from apps.orders.checkout_payment import pay
        from django.db import DatabaseError
        pay(self.order.order_no, self.user)
        player = Player.objects.create(name='guarded', player_type=PlayerType.objects.create(name='guarded', priority=1))
        with self.assertRaises(DatabaseError) as caught:
            with transaction.atomic():
                OrderPlayer.objects.create(order=self.order, player=player)
        self.assertEqual(caught.exception.__cause__.pgcode, '23514')
        self.assertIn('SURCHARGE_LIFECYCLE_UNAVAILABLE', str(caught.exception))
        self.assertEqual(self.order.order_players.count(), 0)
