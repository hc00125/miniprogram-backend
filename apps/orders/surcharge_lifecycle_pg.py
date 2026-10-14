"""Real isolated PG connections. Lock errors are never business rejections."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from decimal import Decimal
from django.test import TransactionTestCase, override_settings
from django.db import connection, connections, transaction, DatabaseError
from rest_framework.exceptions import ValidationError
from . import test_surcharge_lifecycle as fixtures
from .models import Order
from .checkout_payment import pay
from .services import grab_order, start_timer, complete_order
from .surcharge_refunds import cancel_checkout
from apps.earnings.models import PlayerEarning


@override_settings(ORDER_SURCHARGE_ENABLED=True)
class LifecycleConcurrencyTests(TransactionTestCase):
    setUp = fixtures.SurchargeLifecycleTests.setUp
    finish = fixtures.SurchargeLifecycleTests.finish
    add_coin = fixtures.SurchargeLifecycleTests.add_coin

    def race(self, functions):
        self.assertEqual(connection.vendor, 'postgresql')
        gate = Barrier(len(functions))
        def run(fn):
            connections.close_all()
            try:
                db = connections['default'].settings_dict
                assert db['NAME'] == 'test_touchi_backend_v2'
                assert db['USER'] == 'touchi_backend_v2' and db['PORT'] == '55459'
                assert '/backend-pg' in db['HOST']
                with connections['default'].cursor() as cursor:
                    cursor.execute("SET lock_timeout='4s'; SET statement_timeout='8s'")
                gate.wait(timeout=10)
                try:
                    return ('ok', fn())
                except ValidationError as exc:
                    return ('business', exc.detail)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            futures = [pool.submit(run, fn) for fn in functions]
            return [f.result(timeout=15) for f in futures]

    def test_full_lineup_concurrent_finish_generates_one_income_per_source(self):
        pay(self.order.order_no, self.user)
        result = self.race([lambda p=p: grab_order(self.order.order_no, p) for p in self.players])
        self.assertTrue(all(x[0] == 'ok' for x in result), result)
        self.order.refresh_from_db()
        start_timer(self.order, self.players[0])
        result = self.race([lambda p=p: complete_order(self.order, p) for p in self.players])
        self.assertTrue(all(x[0] == 'ok' for x in result), result)
        self.assertEqual(PlayerEarning.objects.filter(order=self.order, source='surcharge').count(), 3)
        self.assertEqual(PlayerEarning.objects.filter(order=self.order, source='order').count(), 3)

    def test_duplicate_cancellation_restores_money_once(self):
        from .surcharge_models import OrderCheckoutRefund
        pay(self.order.order_no, self.user)
        result = self.race([lambda: cancel_checkout(self.order), lambda: cancel_checkout(self.order)])
        self.assertTrue(all(x[0] == 'ok' for x in result), result)
        self.wallet.refresh_from_db(); self.assertEqual(self.wallet.balance, Decimal('50'))
        self.assertEqual(OrderCheckoutRefund.objects.count(), 1)

    def test_cancel_racing_last_grab_never_half_refunds(self):
        pay(self.order.order_no, self.user)
        for player in self.players[:2]: grab_order(self.order.order_no, player)
        result = self.race([lambda: cancel_checkout(self.order),
                            lambda: grab_order(self.order.order_no, self.players[2])])
        self.order.refresh_from_db(); self.wallet.refresh_from_db()
        self.assertEqual(sum(r[0] == 'ok' for r in result), 1, result)
        if self.order.status == Order.STATUS_CANCELLED:
            self.assertEqual(self.wallet.balance, Decimal('50'))
            self.assertEqual(self.order.surcharges.get().status, 'refunded')
        else:
            self.assertEqual(self.order.status, Order.STATUS_READY_TO_START)
            self.assertEqual(self.wallet.balance, Decimal('38.80'))
            self.assertEqual(self.order.surcharges.get().status, 'paid')

    def test_refund_request_and_earning_snapshot_are_immutable(self):
        from .surcharge_models import OrderCheckoutRefund
        pay(self.order.order_no, self.user); cancel_checkout(self.order)
        intent = OrderCheckoutRefund.objects.get()
        with self.assertRaises(DatabaseError) as caught, transaction.atomic():
            OrderCheckoutRefund.objects.filter(pk=intent.pk).update(request_payload={'amount': 999})
        self.assertEqual(caught.exception.__cause__.pgcode, '23514')

    def test_real_room_confirmation_keeps_paid_surcharge_membership(self):
        from django.contrib.auth.models import User
        from rest_framework.test import APIClient
        player = self.players[0]
        user = User.objects.create_user('lifecycle-player')
        player.user = user; player.save(update_fields=['user'])
        pay(self.order.order_no, self.user)
        grab_order(self.order.order_no, player)
        client = APIClient(); client.force_authenticate(user)
        response = client.post('/api/player/order/'+self.order.order_no+'/room-entry/confirm', {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        relation = self.order.order_players.get(player=player)
        self.assertIsNotNone(relation.room_join_confirmed_at)

    def test_prestart_replacement_keeps_equal_required_slots_and_exit_audit(self):
        from .player_cancellations import cancel_player_order
        from apps.players.models import Player
        pay(self.order.order_no, self.user)
        for player in self.players: grab_order(self.order.order_no, player)
        cancel_player_order(self.order.order_no, self.players[0], 'offline replacement')
        replacement = Player.objects.create(name='replacement', player_type=self.players[0].player_type, is_online=True)
        self.order = grab_order(self.order.order_no, replacement)
        self.order = start_timer(self.order, replacement)
        for player in [replacement, *self.players[1:]]:
            self.order = complete_order(self.order, player)
        bonus = PlayerEarning.objects.filter(order=self.order, source='surcharge')
        self.assertEqual(bonus.count(), 3)
        self.assertFalse(bonus.filter(player=self.players[0]).exists())
        self.assertEqual(bonus.get(player=replacement).net_amount, Decimal('3'))
        self.assertTrue(bonus.get(player=replacement).source_snapshot['cancellation_ids'])

    def test_earning_snapshot_and_source_cannot_be_rewritten(self):
        self.finish()
        earning = PlayerEarning.objects.filter(order=self.order, source='surcharge').first()
        for payload in ({'gross_amount': Decimal('99')}, {'source_snapshot': {}}, {'source': 'order', 'surcharge': None}):
            with self.subTest(payload=payload):
                with self.assertRaises(DatabaseError) as caught, transaction.atomic():
                    PlayerEarning.objects.filter(pk=earning.pk).update(**payload)
                self.assertEqual(caught.exception.__cause__.pgcode, '23514')

    def test_competing_withdrawals_and_pay_return_allocate_surcharge_once(self):
        from apps.earnings.services import (release_due_earnings, create_withdrawal,
            approve_withdrawal, return_withdrawal, mark_withdrawal_paid)
        from apps.earnings.models import PlayerWallet, Withdrawal, WithdrawalAllocation
        from datetime import timedelta
        self.finish()
        player = self.players[0]
        deadline = PlayerEarning.objects.filter(order=self.order).first().review_until
        release_due_earnings(now=deadline+timedelta(seconds=1), player=player)
        wallet = PlayerWallet.objects.get(player=player)
        amount = wallet.available_balance
        result = self.race([lambda: create_withdrawal(player, amount, 'wechat', 'offline', 'offline')]*2)
        self.assertEqual(sum(r[0] == 'ok' for r in result), 1, result)
        withdrawal = Withdrawal.objects.get()
        bonus = PlayerEarning.objects.get(order=self.order, player=player, source='surcharge')
        self.assertEqual(withdrawal.allocations.get(earning=bonus).amount, Decimal('3'))
        approve_withdrawal(withdrawal)
        withdrawal.transfer_no='offline-transfer'; withdrawal.save(update_fields=['transfer_no'])
        result = self.race([lambda: mark_withdrawal_paid(withdrawal), lambda: return_withdrawal(withdrawal, 'offline')])
        self.assertEqual(sum(r[0] == 'ok' for r in result), 1, result)
        bonus.refresh_from_db(); wallet.refresh_from_db(); withdrawal.refresh_from_db()
        self.assertIn(withdrawal.status, ('paid', 'rejected'))
        self.assertEqual(bonus.available_amount+bonus.withdrawing_amount+bonus.withdrawn_amount, Decimal('3'))
        self.assertEqual(wallet.withdrawing_balance, Decimal('0'))
        active = WithdrawalAllocation.objects.filter(earning=bonus,
            withdrawal__status__in=('pending_review','pending_payment','paid'))
        self.assertLessEqual(sum(a.amount for a in active), bonus.net_amount-bonus.reversed_amount)

    def test_duplicate_remote_refund_dispatch_has_one_committed_external_call(self):
        from unittest.mock import patch
        from apps.wallet.coin_sync import sync_pending_coin_refunds, coin_backed_wallet_amount
        from .surcharge_models import OrderCheckoutRefund
        class Spend:
            def authenticate(self, attempt, code): return 'offline'
            def spend(self, attempt, session): return {'errcode': 0, 'order_id': attempt.external_id}
        self.add_coin(); pay(self.order.order_no, self.user, adapter=Spend()); cancel_checkout(self.order)
        lock = Lock(); calls = []
        def external(path, payload, session, **kwargs):
            self.assertFalse(connections['default'].in_atomic_block)
            # Independent connection observes dispatch intent committed BEFORE external effect.
            import psycopg2
            db = connections['default'].settings_dict
            with psycopg2.connect(dbname=db['NAME'], user=db['USER'], host=db['HOST'], port=db['PORT']) as other:
                with other.cursor() as cursor:
                    cursor.execute('SELECT remote_status, request_payload FROM orders_ordercheckoutrefund')
                    status, saved = cursor.fetchone()
                    self.assertEqual(status, 'dispatching'); self.assertEqual(saved, payload)
            with lock: calls.append(payload)
            return {'errcode': 0}
        with patch('apps.wallet.coin_sync.user_xpay_post', side_effect=external):
            result = self.race([lambda: sync_pending_coin_refunds(self.profile, 'offline')]*2)
        self.assertTrue(all(x[0] == 'ok' for x in result), result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(OrderCheckoutRefund.objects.get().remote_status, 'succeeded')
        self.assertEqual(coin_backed_wallet_amount(self.profile), Decimal('50'))
