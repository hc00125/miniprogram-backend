"""Explicit PostgreSQL tests; v1 standalone append assertions migrated to v2.

No deadlock/timeout is accepted as protection. Retired append route is tested
as refused; original payment/race/recovery coverage now enters real checkout.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from decimal import Decimal
from django.contrib.auth.models import User
from django.db import connection, connections, transaction, DatabaseError
from django.test import TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError
from apps.catalog.models import Package, PlayerType
from apps.players.models import Player
from apps.orders.models import Order, OrderPlayer
from apps.orders import test_surcharge_v2 as fixtures


@override_settings(WECHAT_VIRTUALPAY_ENV=1, ORDER_SURCHARGE_ENABLED=True)
class SurchargePaymentPGTests(TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, 'postgresql')
        fixtures.CheckoutPaymentTests.setUp(self)

    def service(self):
        from apps.orders import checkout_payment
        return checkout_payment

    def race(self, *functions):
        barrier = Barrier(len(functions))
        def run(fn):
            connections.close_all()
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute("SET lock_timeout='4s'")
                barrier.wait(timeout=10)
                return fn()
            except ValidationError:
                return 'blocked'
            except DatabaseError as exc:
                if getattr(exc.__cause__, 'pgcode', None) != '23514' or not any(code in str(exc) for code in (
                        'SURCHARGE_PAYMENT_PENDING', 'SURCHARGE_LIFECYCLE_UNAVAILABLE')):
                    raise
                return 'blocked'
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            futures = [pool.submit(run, fn) for fn in functions]
            return [future.result(timeout=15) for future in futures]

    def test_missing_internal_configuration_does_not_block_fixed_surcharge(self):
        from apps.gifts.models import Gift, PlayerGiftConfig
        Gift.objects.filter(code='order_surcharge').update(internal_enabled=False)
        self.assertFalse(PlayerGiftConfig.objects.exists())
        result = self.service().pay(self.order.order_no, self.user)
        self.assertEqual(result['status'], 'paid')
        self.assertEqual(result['checkout']['surcharge']['commission_rate'], '25.00')

    def test_two_checkouts_cannot_reserve_same_wallet(self):
        from apps.orders.duration_services import create_order
        from apps.orders.checkout import quote_order
        data = {'boss_wechat': 'second', 'package_id': self.order.package_id, 'surcharge_diamonds': 12}
        data.update(quote_version=quote_order(data)['quote_version'], idempotency_key='second')
        second = create_order(data, self.user)
        result = self.race(*[lambda number=number: self.service().prepare(number,
            User.objects.get(pk=self.user.pk)).status for number in (self.order.order_no, second.order_no)])
        self.assertCountEqual(result, ['processing', 'blocked'])

    def test_real_grab_races_checkout_prepare(self):
        from apps.orders.services import grab_order
        player = Player.objects.create(name='grabber', player_type=PlayerType.objects.create(name='grab', priority=1))
        result = self.race(
            lambda: self.service().prepare(self.order.order_no, User.objects.get(pk=self.user.pk)).status,
            lambda: (grab_order(self.order.order_no, Player.objects.get(pk=player.pk)), 'grabbed')[1])
        self.assertCountEqual(result, ['processing', 'blocked'])
        self.assertEqual(self.order.order_players.count(), 0)

    def test_real_cancel_races_checkout_prepare(self):
        from apps.orders.services import cancel_order
        def cancel():
            with transaction.atomic():
                cancel_order(Order.objects.select_for_update().get(pk=self.order.pk), reason='synthetic')
            return 'cancelled'
        result = self.race(
            lambda: self.service().prepare(self.order.order_no, User.objects.get(pk=self.user.pk)).status,
            cancel)
        self.assertEqual(result.count('blocked'), 1, result)

    def test_retired_standalone_post_refused_and_existing_order_unchanged(self):
        url = '/api/boss/orders/' + self.order.order_no + '/surcharge/'
        response = self.client.post(url, {'amount_diamonds': 12, 'idempotency_key': 'api'}, format='json')
        self.assertFalse(self.client.get(url).data['can_submit'])
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data['code'], 'SURCHARGE_PREORDER_ONLY')
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('30'))
        self.assertEqual(self.order.surcharges.count(), 1)

    def test_combined_payment_preserves_base_amount_and_replay(self):
        from apps.payments.models import Payment
        result = self.service().pay(self.order.order_no, self.user)
        replay = self.service().pay(self.order.order_no, self.user)
        self.assertEqual(result['status'], 'paid')
        self.assertEqual(replay['payment_no'], result['payment_no'])
        self.order.refresh_from_db(); self.wallet.refresh_from_db()
        self.assertTrue(self.order.paid)
        self.assertEqual(self.wallet.balance, Decimal('18.80'))
        self.assertEqual(Payment.objects.get(order=self.order).amount, Decimal('10'))
        self.assertEqual(self.order.surcharges.get().amount_yuan, Decimal('1.20'))

    def test_prepared_recovery_releases_reservation_but_never_grants_unpaid_bonus(self):
        from apps.wallet.spend_service import recover
        from apps.orders.services import grab_order
        from apps.orders.serializers import AvailableOrderSerializer
        item = self.service().prepare(self.order.order_no, self.user)
        result = recover(item.attempt_id)
        self.assertEqual(result.status, 'failed'); self.assertEqual(result.reserved_amount, 0)
        self.assertEqual(self.order.surcharges.get().status, 'failed')
        self.assertEqual(AvailableOrderSerializer(self.order).data['surcharge']['paid_diamonds'], 0)
        player = Player.objects.create(name='after', player_type=PlayerType.objects.create(name='after', priority=1))
        with self.assertRaises(ValidationError): grab_order(self.order.order_no, player)
        # An independent legacy order remains eligible after reservation release.
        plain = Order.objects.create(order_no='plain-after', boss_user=self.user,
            package=Package.objects.create(name='plain', base_price=10), required_players=1)
        grab_order(plain.order_no, player)
        self.assertTrue(plain.order_players.filter(player=player).exists())

    def test_prepared_disabled_first_dispatch_is_rejected(self):
        item = self.service().prepare(self.order.order_no, self.user)
        with override_settings(ORDER_SURCHARGE_ENABLED=False):
            with self.assertRaises(ValidationError): self.service().pay(self.order.order_no, self.user)
        item.attempt.refresh_from_db(); self.wallet.refresh_from_db()
        self.assertEqual(self.order.surcharges.get().status, 'failed')
        self.assertEqual(item.attempt.reserved_amount, 0)
        self.assertEqual(self.wallet.balance, Decimal('30'))

    def test_lifecycle_incomplete_is_a_hard_non_test_payment_gate(self):
        # Lifecycle admission is now explicit, never the offline test flag.
        with override_settings(ORDER_SURCHARGE_ISOLATED_TEST_MODE=False,
                               ORDER_SURCHARGE_CHECKOUT_READY=False):
            with self.assertRaises(ValidationError) as caught:
                self.service().prepare(self.order.order_no, self.user)
        self.assertEqual(str(caught.exception.detail['code']), 'SURCHARGE_LIFECYCLE_UNAVAILABLE')
        self.assertIsNone(self.order.checkout.attempt_id)

    def test_paid_surcharge_raw_lineup_and_completion_are_fail_closed(self):
        from apps.earnings.models import PlayerEarning
        self.service().pay(self.order.order_no, self.user)
        player = Player.objects.create(name='raw', player_type=PlayerType.objects.create(name='raw', priority=1))
        for _ in range(2):
            with self.assertRaises(DatabaseError) as caught, transaction.atomic():
                OrderPlayer.objects.create(order=self.order, player=player)
            self.assertEqual(caught.exception.__cause__.pgcode, '23514')
            self.assertIn('SURCHARGE_LIFECYCLE_UNAVAILABLE', str(caught.exception))
            with self.assertRaises(DatabaseError) as caught, transaction.atomic():
                Order.objects.filter(pk=self.order.pk).update(status=Order.STATUS_COMPLETED)
            self.assertEqual(caught.exception.__cause__.pgcode, '23514')
            self.assertIn('SURCHARGE_LIFECYCLE_UNAVAILABLE', str(caught.exception))
        self.assertFalse(PlayerEarning.objects.filter(order=self.order).exists())

    def test_pending_surcharge_blocks_raw_lineup_and_cancel_writes(self):
        item = self.service().prepare(self.order.order_no, self.user)
        self.assertEqual(item.status, 'processing')
        player = Player.objects.create(name='pending', player_type=PlayerType.objects.create(name='pending', priority=1))
        with self.assertRaises(DatabaseError) as lineup_error, transaction.atomic():
            OrderPlayer.objects.create(order=self.order, player=player)
        self.assertEqual(lineup_error.exception.__cause__.pgcode, '23514')
        self.assertIn('SURCHARGE_LIFECYCLE_UNAVAILABLE', str(lineup_error.exception))
        with self.assertRaises(DatabaseError) as cancel_error, transaction.atomic():
            Order.objects.filter(pk=self.order.pk).update(status=Order.STATUS_CANCELLED)
        self.assertEqual(cancel_error.exception.__cause__.pgcode, '23514')
        self.assertIn('SURCHARGE_PAYMENT_PENDING', str(cancel_error.exception))
        self.assertEqual(self.order.surcharges.get().status, 'processing')
