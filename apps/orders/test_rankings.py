"""Public completed-service rankings; run only with offline isolated settings."""
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from apps.catalog.models import Package, PlayerType
from apps.orders.models import Order, OrderPlayer
from apps.orders.services import complete_order
from apps.players.models import Player

BEIJING = ZoneInfo('Asia/Shanghai')
NOW = datetime(2026, 10, 7, 12, tzinfo=BEIJING)


@override_settings(RANKINGS_ENABLED=True)
class RankingsTests(TestCase):
    def setUp(self):
        self.clock = patch('django.utils.timezone.now', return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.kind = PlayerType.objects.create(name='普通', priority=1)
        self.package = Package.objects.create(name='排行测试', player_count=1, base_price=10)
        self.player = Player.objects.create(name='公开陪玩', player_type=self.kind)
        self.serial = 0

    def order(self, player=None, status=Order.STATUS_IN_PROGRESS, member='已接单', at=NOW, **kwargs):
        self.serial += 1
        order = Order.objects.create(order_no=f'RANK{self.serial}', package=self.package,
                                     required_players=1, status=status, paid=True, total_amount=10, **kwargs)
        relation = OrderPlayer.objects.create(order=order, player=player or self.player, status=member)
        OrderPlayer.objects.filter(pk=relation.pk).update(grab_time=at)
        return order

    def get(self, period='day', board='player'):
        result = self.client.get('/api/rankings/', {'board': board, 'period': period})
        self.assertEqual(result.status_code, 200)
        return result.json()

    def test_boss_paid_order_net_refunds_use_original_payment_period(self):
        from decimal import Decimal
        from django.contrib.auth.models import User
        from apps.payments.models import Payment, Refund
        boss = User.objects.create_user(username='private-boss-login')
        order = self.order(boss_user=boss)
        payment = Payment.objects.create(order=order, payment_no='RANKPAY', channel='balance',
                                         scene='balance', amount=Decimal('12.35'), status='paid', paid_at=NOW)
        Refund.objects.create(order=order, payment=payment, refund_no='RANKREF',
                              amount=Decimal('2.01'), status='succeeded')
        Refund.objects.create(order=order, payment=payment, refund_no='PENDINGREF',
                              amount=Decimal('9'), status='pending')
        Payment.objects.create(order=order, payment_no='UNPAID', channel='balance',
                               scene='balance', amount=100, status='paying')
        body = self.get(board='boss')
        self.assertTrue(body['available'])
        self.assertEqual(body['results'][0]['name'], '老板')
        self.assertEqual(body['results'][0]['value'], 103.4)
        Payment.objects.filter(pk=payment.pk).update(paid_at=datetime(2026, 10, 6, 12, tzinfo=BEIJING))
        self.assertEqual(self.get(board='boss')['results'], [])
        self.assertEqual(self.get(board='boss', period='week')['results'][0]['value'], 103.4)
        Refund.objects.create(order=order, payment=payment, refund_no='FULLREF',
                              amount=Decimal('10.34'), status='succeeded')
        self.assertEqual(self.get(board='boss', period='week')['results'], [])

    def spend(self, boss, kind, no, amount, state='completed'):
        from decimal import Decimal
        from apps.accounts.models import ClientProfile
        from apps.wallet.models import ClientWallet, ClientWalletLedger, WalletSpendAttempt
        profile, _ = ClientProfile.objects.get_or_create(user=boss, defaults={'openid': f'openid-{boss.pk}', 'nickname': f'老板{boss.pk}'})
        wallet, _ = ClientWallet.objects.get_or_create(profile=profile)
        attempt = WalletSpendAttempt.objects.create(wallet=wallet, kind=kind, business_no=no,
            idempotency_key=no, request_digest='a'*64, amount=Decimal(amount),
            reserved_amount=0 if state in ('completed', 'failed') else Decimal(amount), status=state)
        ledger = ClientWalletLedger.objects.create(wallet=wallet, entry_type='shared_spend',
            amount=-Decimal(amount), balance_after=0, reference_type=kind, reference_id=attempt.external_id)
        return attempt, ledger

    def test_completed_patronage_is_paid_consumption_not_expiry_or_bonus(self):
        from datetime import timedelta
        from decimal import Decimal
        from django.contrib.auth.models import User
        from apps.patronage.models import PatronagePurchase, CrownGrant
        boss = User.objects.create_user(username='patron-boss')
        for i, (state, status) in enumerate([('completed', 'paid'), ('failed', 'failed'), ('unknown', 'unknown')]):
            attempt, _ = self.spend(boss, 'patronage', f'PNRANK{i}', '18.80', state)
            purchase = PatronagePurchase.objects.create(purchase_no=f'PNRANK{i}', boss=boss, player=self.player,
                player_name=self.player.name, package_code='week', package_name='周冠',
                amount_yuan=Decimal('18.80'), commission_rate=Decimal('0.25'),
                platform_amount_yuan=Decimal('4.70'), player_amount_yuan=Decimal('14.10'),
                price_version='a'*64, config_snapshot={}, idempotency_key=f'PNRANK{i}', attempt=attempt,
                payment_status=status, paid_at=NOW if status == 'paid' else None)
            if status == 'paid':
                CrownGrant.objects.create(purchase=purchase, source='purchase', package_name='周冠',
                    starts_at=NOW, expires_at=NOW+timedelta(days=7), revoked_at=NOW)
        body = self.get(board='boss')
        self.assertEqual(body['results'][0]['value'], 188)
        # Revocation and expiry are not evidence of an offline refund.
        self.assertEqual(self.get(board='boss')['results'], body['results'])

    def test_gifts_count_paid_purchase_once_at_debit_time_minus_completed_refunds(self):
        from django.contrib.auth.models import User
        from apps.gifts.models import Gift, GiftPurchase, GiftRefund, GiftTransfer
        from apps.wallet.models import ClientWalletLedger
        boss = User.objects.create_user(username='gift-boss')
        gift = Gift.objects.create(name='礼物', price_diamonds=100)
        attempt, ledger = self.spend(boss, 'gift', 'GPRANK', '10')
        purchase = GiftPurchase.objects.create(purchase_no='GPRANK', buyer=boss, gift=gift,
            quantity=2, snapshot={'name': '礼物'}, idempotency_key='GPRANK', request_digest='a'*64,
            attempt=attempt, status='paid')
        GiftRefund.objects.create(refund_no='GREF', purchase=purchase, quantity=1, diamonds=20,
            requested_by=boss, reason='成功退款', status='completed')
        GiftRefund.objects.create(refund_no='GREFPENDING', purchase=purchase, quantity=1, diamonds=80,
            requested_by=boss, reason='尚未完成', status='approved')
        GiftTransfer.objects.create(transfer_no='INVENTORYSEND', sender=boss, recipient=self.player,
            gift=gift, quantity=1, source='inventory', idempotency_key='send', request_digest='a'*64,
            commission_snapshot={'rate': '25'}, status='delivered')
        GiftPurchase.objects.create(purchase_no='FREE', buyer=boss, gift=gift, quantity=1,
            snapshot={'name': '免费'}, idempotency_key='FREE', request_digest='a'*64, status='paid')
        body = self.get(board='boss')
        self.assertEqual([r['value'] for r in body['results']], [80])
        ClientWalletLedger.objects.filter(pk=ledger.pk).update(created_at=datetime(2026, 10, 6, 12, tzinfo=BEIJING))
        self.assertEqual(self.get(board='boss')['results'], [])
        self.assertEqual(self.get(board='boss', period='week')['results'][0]['value'], 80)

    def test_checkout_base_and_surcharge_split_not_total_twice(self):
        from django.contrib.auth.models import User
        from apps.payments.models import Payment
        from apps.orders.surcharge_models import OrderSurcharge, OrderCheckout
        boss = User.objects.create_user(username='checkout-boss')
        order = Order.objects.create(order_no='CHECKOUTRANK', boss_user=boss, package=self.package,
            required_players=1, surcharge_guarded=True)
        attempt, _ = self.spend(boss, 'order_checkout', order.order_no, '15')
        surcharge = OrderSurcharge.objects.create(surcharge_no='SRANK', order=order, boss=boss,
            amount_diamonds=50, amount_yuan=5, status='paid', idempotency_key='SKEY', request_digest='a'*64)
        OrderCheckout.objects.create(order=order, boss=boss, surcharge=surcharge, attempt=attempt,
            idempotency_key='CKEY', request_digest='a'*64, base_amount=10,
            surcharge_amount=5, total_amount=15, status='paid')
        Payment.objects.create(order=order, payment_no='CPAY', channel='balance', scene='balance',
                               amount=10, status='paid', paid_at=NOW)
        self.assertEqual([r['value'] for r in self.get(board='boss')['results']], [150])
        OrderSurcharge.objects.filter(pk=surcharge.pk).update(status='refunded', refunded_diamonds=50)
        self.assertEqual(self.get(board='boss')['results'][0]['value'], 100)

    def test_cancelled_and_incomplete_members_never_count(self):
        from apps.orders.services import cancel_order
        from apps.orders.player_cancellations import cancel_player_order
        for state in (Order.STATUS_WAITING, Order.STATUS_PENDING_PAYMENT, Order.STATUS_READY_TO_START,
                      Order.STATUS_IN_PROGRESS, Order.STATUS_CANCELLED):
            self.order(status=state, member='已完成')
        for member in ('已接单', '订单已取消', '已退出'):
            self.order(status=Order.STATUS_COMPLETED, member=member)
        waiting = self.order(status=Order.STATUS_WAITING)
        Order.objects.filter(pk=waiting.pk).update(paid=False)
        waiting.refresh_from_db()
        cancel_order(waiting)
        leaving = self.order(status=Order.STATUS_WAITING)
        Order.objects.filter(pk=leaving.pk).update(paid=False)
        cancel_player_order(leaving.order_no, self.player, '不再参与')
        self.assertFalse(leaving.order_players.exists())
        self.assertEqual(self.get()['results'], [])

    def test_multiplayer_final_gate_and_individual_qualification(self):
        second = Player.objects.create(name='第二人', player_type=self.kind)
        third = Player.objects.create(name='退出者', player_type=self.kind)
        order = self.order()
        order.required_players = 2
        order.save(update_fields=['required_players'])
        OrderPlayer.objects.create(order=order, player=second)
        complete_order(order, self.player)
        self.assertEqual(self.get()['results'], [])
        complete_order(order, second)
        self.assertEqual({r['name']: r['value'] for r in self.get()['results']}, {'公开陪玩': 1, '第二人': 1})
        OrderPlayer.objects.create(order=order, player=third, status='订单已取消')
        self.assertEqual(len(self.get()['results']), 2)
        from apps.orders.models import OrderStatusLog
        for _ in range(3):
            OrderStatusLog.objects.create(order=order, to_status=Order.STATUS_COMPLETED, reason='重复日志')
        self.assertEqual([r['value'] for r in self.get()['results']], [1, 1])

    def test_top50_competition_ranking_stable_and_select_only(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        for i in range(52):
            player = Player.objects.create(name=f'玩家{i:02}', player_type=self.kind)
            for _ in range(2 if i < 2 else 1):
                self.order(player=player, status=Order.STATUS_COMPLETED, member='已完成')
        with CaptureQueriesContext(connection) as sql:
            rows = self.get()['results']
        self.assertEqual(len(rows), 50)
        self.assertEqual([r['rank'] for r in rows[:3]], [1, 1, 3])
        self.assertEqual(rows, self.get()['results'])
        self.assertLessEqual(len(sql), 2)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in sql))

    def test_http_contract_anonymous_readonly_and_disabled(self):
        for board in ('boss', 'player'):
            response = self.client.get('/api/rankings/', {'board': board, 'period': 'day'}, HTTP_AUTHORIZATION='Bearer invalid')
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()['available'])
            self.assertEqual(response.json()['results'], [])
        with self.assertNumQueries(0):
            self.assertEqual(self.client.post('/api/rankings/').status_code, 405)
            self.assertEqual(self.client.get('/api/rankings/', {'period': 'year'}).status_code, 400)
            self.assertEqual(self.client.get('/api/rankings/', {'board': 'private'}).status_code, 400)
        with override_settings(RANKINGS_ENABLED=False), self.assertNumQueries(0):
            self.assertEqual(self.get(), {'available': False})
            self.assertEqual(self.get(board='boss'), {'available': False})

    def test_boss_competition_top50_select_only_and_minimal_fields(self):
        from decimal import Decimal
        from django.contrib.auth.models import User
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from apps.payments.models import Payment
        for i in range(52):
            boss = User.objects.create_user(username=f'private-login-{i}')
            order = self.order(boss_user=boss)
            Payment.objects.create(order=order, payment_no=f'TOP{i}', channel='balance', scene='balance',
                                   amount=Decimal('1.01') if i < 2 else Decimal('1.00'), status='paid', paid_at=NOW)
        with CaptureQueriesContext(connection) as queries:
            rows = self.get(board='boss')['results']
        self.assertEqual(len(rows), 50)
        self.assertEqual([r['rank'] for r in rows[:3]], [1, 1, 3])
        self.assertEqual([r['value'] for r in rows[:3]], [10.1, 10.1, 10])
        self.assertLessEqual(len(queries), 5)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.assertTrue(all(set(r) == {'id', 'rank', 'name', 'avatar_url', 'value'} for r in rows))
        self.assertNotIn('private-login', str(rows))

    def test_same_boss_sources_merge_day_pass_bonus_and_recharge_do_not_double_count(self):
        from decimal import Decimal
        from datetime import timedelta
        from django.contrib.auth.models import User
        from apps.payments.models import Payment
        from apps.gifts.models import Gift, GiftPurchase
        from apps.patronage.models import PatronagePurchase, CrownGrant
        from apps.wallet.models import ClientWalletLedger
        boss = User.objects.create_user(username='merged-boss')
        order = self.order(boss_user=boss)
        Payment.objects.create(order=order, payment_no='MERGEDPAY', channel='balance', scene='balance',
                               amount=10, status='paid', paid_at=NOW)
        gift = Gift.objects.create(name='合并礼物', price_diamonds=20)
        attempt, _ = self.spend(boss, 'gift', 'MERGEDGIFT', '2')
        GiftPurchase.objects.create(purchase_no='MERGEDGIFT', buyer=boss, gift=gift, quantity=1,
            snapshot={'name': '合并礼物'}, idempotency_key='MERGEDGIFT', request_digest='a'*64,
            status='paid', attempt=attempt)
        attempt, _ = self.spend(boss, 'patronage', 'MERGEDDAYPASS', '4')
        purchase = PatronagePurchase.objects.create(purchase_no='MERGEDDAYPASS', boss=boss, player=self.player,
            player_name=self.player.name, package_code='day_pass', package_name='包天', bonus_naming_days=7,
            amount_yuan=Decimal('4'), commission_rate=Decimal('.25'), platform_amount_yuan=1,
            player_amount_yuan=3, price_version='a'*64, config_snapshot={}, idempotency_key='MERGEDDAYPASS',
            attempt=attempt, payment_status='paid', paid_at=NOW)
        CrownGrant.objects.create(purchase=purchase, source='day_pass_bonus', package_name='赠冠',
            starts_at=NOW, expires_at=NOW+timedelta(days=7))
        ClientWalletLedger.objects.create(wallet=attempt.wallet, entry_type='recharge', amount=1000,
            balance_after=1000, reference_type='recharge', reference_id='NOTCONSUMPTION')
        self.assertEqual([r['value'] for r in self.get(board='boss')['results']], [160])

    def test_real_grab_time_survives_completion_without_counting_lifetime_counter(self):
        from datetime import timedelta
        from apps.orders.services import grab_order, start_timer
        self.player.is_online = True
        self.player.save(update_fields=['is_online'])
        order = Order.objects.create(order_no='REALGRAB', package=self.package, required_players=1,
            paid=True, total_amount=10, status=Order.STATUS_WAITING)
        accepted_at = NOW - timedelta(days=1)
        with patch('django.utils.timezone.now', return_value=accepted_at):
            order = grab_order(order.order_no, self.player)
        self.assertEqual(order.order_players.get().grab_time, accepted_at)
        self.assertEqual(self.get(period='week')['results'], [])
        order = start_timer(order, self.player)
        complete_order(order, self.player)
        self.assertEqual(order.order_players.get().grab_time, accepted_at)
        self.assertEqual(self.get()['results'], [])
        self.assertEqual(self.get(period='week')['results'][0]['value'], 1)
        Player.objects.filter(pk=self.player.pk).update(total_orders=999999)
        self.assertEqual(self.get(period='week')['results'][0]['value'], 1)

    def test_historical_standalone_surcharge_uses_verified_debit_and_successful_refund_total(self):
        from django.contrib.auth.models import User
        from apps.orders.surcharge_models import OrderSurcharge
        boss = User.objects.create_user(username='standalone-boss')
        order = Order.objects.create(order_no='STANDALONE', boss_user=boss, package=self.package,
            required_players=1, surcharge_guarded=True)
        attempt, _ = self.spend(boss, 'order_surcharge', 'STANDALONES', '2')
        surcharge = OrderSurcharge.objects.create(surcharge_no='STANDALONES', order=order, boss=boss,
            amount_diamonds=20, amount_yuan=2, status='paid', attempt=attempt,
            idempotency_key='STANDALONES', request_digest='a'*64)
        self.assertEqual([r['value'] for r in self.get(board='boss')['results']], [20])
        OrderSurcharge.objects.filter(pk=surcharge.pk).update(status='partially_refunded', refunded_diamonds=5)
        self.assertEqual(self.get(board='boss')['results'][0]['value'], 15)

    def test_actual_http_responses_for_frontend_integration(self):
        import json
        import os
        from pathlib import Path
        from decimal import Decimal
        from django.contrib.auth.models import User
        from apps.accounts.models import ClientProfile
        from apps.payments.models import Payment
        boss = User.objects.create_user(username='integration-private')
        ClientProfile.objects.create(user=boss, nickname='联调老板', openid='integration-private-openid')
        order = self.order(boss_user=boss)
        complete_order(order, self.player)
        Payment.objects.create(order=order, payment_no='INTEGRATIONPAY', channel='balance', scene='balance',
                               amount=Decimal('12.35'), status='paid', paid_at=NOW)
        responses = {}
        for board in ('boss', 'player'):
            for period in ('day', 'week', 'month'):
                response = self.client.get('/api/rankings/', {'board': board, 'period': period})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()['results']), 1)
                responses[f'{board}:{period}'] = {'statusCode': response.status_code, 'data': response.json()}
        if os.environ.get('RANKINGS_CAPTURE_PATH'):
            Path(os.environ['RANKINGS_CAPTURE_PATH']).write_text(json.dumps(responses, ensure_ascii=False, indent=2))

    def test_private_profiles_and_unsafe_avatars_are_not_disclosed(self):
        from django.contrib.auth.models import User
        from apps.accounts.models import ClientProfile
        user = User.objects.create_user(username='never-public-login')
        ClientProfile.objects.create(user=user, openid='secret-openid', nickname='昵称', avatar_url='http://unsafe.invalid/a')
        self.player.user = user
        self.player.save(update_fields=['user'])
        self.order(status=Order.STATUS_COMPLETED, member='已完成')
        hidden = Player.objects.create(name='隐藏', player_type=self.kind, is_publicly_visible=False)
        self.order(player=hidden, status=Order.STATUS_COMPLETED, member='已完成')
        body = self.get()
        self.assertEqual(len(body['results']), 1)
        self.assertEqual(set(body['results'][0]), {'id', 'rank', 'name', 'avatar_url', 'value'})
        self.assertEqual(body['results'][0]['avatar_url'], '')
        self.assertNotIn('secret-openid', str(body))
        self.assertNotIn('never-public-login', str(body))

    def test_periods_use_acceptance_in_beijing_not_completion(self):
        for date in ('2026-09-30T23:59:59+08:00', '2026-10-01T00:00:00+08:00',
                     '2026-10-04T23:59:59+08:00', '2026-10-05T00:00:00+08:00',
                     '2026-10-06T15:59:59+00:00', '2026-10-06T16:00:00+00:00',
                     '2026-10-07T15:59:59+00:00', '2026-10-07T16:00:00+00:00'):
            self.order(status=Order.STATUS_COMPLETED, member='已完成', at=datetime.fromisoformat(date))
        for period, expected, start, end in (
            ('day', 2, '2026-10-07T00:00:00+08:00', '2026-10-08T00:00:00+08:00'),
            ('week', 5, '2026-10-05T00:00:00+08:00', '2026-10-12T00:00:00+08:00'),
            ('month', 7, '2026-10-01T00:00:00+08:00', '2026-11-01T00:00:00+08:00')):
            with self.subTest(period=period):
                body = self.get(period)
                self.assertEqual(body['results'][0]['value'], expected)
                self.assertEqual((body['period_start'], body['period_end']), (start, end))

    def test_renewal_copy_is_not_another_completed_service(self):
        from apps.orders.models import OrderItem
        from apps.orders.renewals import create_renewal_order, finalize_paid_renewal
        from django.contrib.auth.models import User
        owner = User.objects.create_user(username='renewal-owner')
        root = self.order(boss_user=owner)
        OrderItem.objects.create(order=root, package=self.package, package_name='排行测试',
                                 unit_price=10, quantity=1, amount=10)
        renewal, created = create_renewal_order(root.order_no, owner)
        self.assertTrue(created)
        self.assertEqual(renewal.order_players.get().status, '已接单')
        renewal.paid = True
        renewal.save(update_fields=['paid'])
        finalize_paid_renewal(renewal)
        renewal.refresh_from_db()
        self.assertEqual(renewal.status, Order.STATUS_COMPLETED)
        self.assertEqual(self.get()['results'], [])
        complete_order(root, self.player)
        # Even a later manual member-status edit cannot turn the duration purchase
        # into an independent service completion or a second acceptance event.
        renewal.order_players.update(status='已完成')
        self.assertEqual(self.get()['results'][0]['value'], 1)

    def test_real_completion_is_required_not_merely_acceptance(self):
        order = self.order()
        self.assertEqual(self.get()['results'], [])
        complete_order(order, self.player)
        body = self.get()
        self.assertTrue(body['available'])
        self.assertEqual([(r['name'], r['value']) for r in body['results']], [('公开陪玩', 1)])
        self.assertEqual(self.get()['results'], body['results'])
