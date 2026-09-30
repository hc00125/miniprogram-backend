"""Staff cancellation is an order transition, never an automatic refund."""
import json
from unittest.mock import patch
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from apps.orders.models import Order, OrderPlayer, OrderStatusLog
from apps.orders import test_dispatch


class DispatchCancellationTests(TestCase):
    setUp = test_dispatch.DispatchTests.setUp
    post = test_dispatch.DispatchTests.post
    make_dispatch = test_dispatch.DispatchTests.make_dispatch

    def cancel(self, order, **payload):
        return self.post('orders/' + order.order_no + '/cancel', payload or {'reason': '老板临时取消'})

    def financial_snapshot(self):
        labels = ['wallet.ClientWallet', 'wallet.ClientWalletLedger', 'earnings.PlayerWallet',
                  'earnings.PlayerEarning', 'earnings.WalletLedger', 'earnings.WalletAdjustment',
                  'payments.Payment', 'payments.Refund', 'accounts.BossConsumptionLedger',
                  'dispatch.DispatchReceipt']
        return {label: list(apps.get_model(label).objects.order_by('pk').values()) for label in labels}

    def player(self):
        from apps.catalog.models import PlayerType
        from apps.players.models import Player
        kind = PlayerType.objects.create(name='取消测试陪玩类型', priority=0)
        return Player.objects.create(name='取消测试陪玩', player_type=kind, status='approved', is_online=True)

    def test_waiting_cancel_keeps_receipt_money_and_audit(self):
        from apps.orders.kook_notifications import public_open
        from apps.orders.services import grab_order
        order, _, _ = self.make_dispatch()
        before = self.financial_snapshot()
        response = self.cancel(order)
        self.assertEqual(response.status_code, 200, response.content[:300])
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        self.assertEqual(order.cancel_reason, '老板临时取消')
        self.assertIsNotNone(order.canceled_at)
        self.assertTrue(order.paid)
        self.assertEqual(order.payment_method, 'staff_offline')
        self.assertEqual(order.total_amount, 20)
        self.assertFalse(public_open(order))
        self.assertEqual(response.json()['offline_refund_status'], 'unconfirmed')
        self.assertEqual(self.financial_snapshot(), before)
        log = OrderStatusLog.objects.get(order=order, to_status=Order.STATUS_CANCELLED)
        self.assertEqual(log.operator_id, self.agent.pk)
        self.assertIn('线下退款未确认', log.reason)
        with self.assertRaises(ValidationError):
            grab_order(order.order_no, self.player())

    def test_registered_boss_and_player_existing_balances_are_unchanged(self):
        from apps.accounts.models import ClientProfile
        from apps.wallet.models import ClientWallet
        from apps.earnings.models import PlayerWallet
        from apps.orders.services import grab_order
        order, _, _ = self.make_dispatch()
        boss = get_user_model().objects.create_user(username='cancel-boss')
        profile = ClientProfile.objects.create(user=boss, openid='cancel-boss-fixture', nickname='已有余额老板')
        ClientWallet.objects.create(profile=profile, balance='123.45', recharged_total='150.00', spent_total='26.55')
        order.boss_user = boss; order.save(update_fields=['boss_user'])
        player = self.player()
        PlayerWallet.objects.create(player=player, available_balance='72.60', pending_balance='12.30', debt_balance='1.20')
        order = grab_order(order.order_no, player)
        before = self.financial_snapshot()
        response = self.cancel(order)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(self.financial_snapshot(), before)

    def test_repeat_does_not_duplicate_logs_or_overwrite_first_reason(self):
        order, _, _ = self.make_dispatch()
        first = self.cancel(order)
        self.assertEqual(first.status_code, 200, first.content[:300])
        order.refresh_from_db(); timestamp = order.canceled_at
        again = self.cancel(order, reason='重试不应覆盖原原因')
        self.assertEqual(again.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.canceled_at, timestamp)
        self.assertEqual(order.cancel_reason, '老板临时取消')
        self.assertEqual(OrderStatusLog.objects.filter(order=order, to_status=Order.STATUS_CANCELLED).count(), 1)

    def test_ready_cancel_retains_lineup_audit_without_penalties(self):
        from apps.orders.services import grab_order, start_timer
        from apps.orders.cancellation_models import PlayerDiscipline, PlayerCancellationRecord, OrderReplacementState
        from apps.orders.models import OrderDesignation
        from datetime import timedelta
        order, _, _ = self.make_dispatch(); player = self.player()
        order = grab_order(order.order_no, player)
        self.assertEqual(order.status, Order.STATUS_READY_TO_START)
        state = OrderReplacementState.objects.create(order=order, mode='public', status='open', missing_slots=1)
        designation = OrderDesignation.objects.create(order=order, player=player, status='accepted', expires_at=timezone.now()+timedelta(hours=1))
        before = self.financial_snapshot()
        response = self.cancel(order)
        self.assertEqual(response.status_code, 200, response.content[:300])
        relation = OrderPlayer.objects.get(order=order, player=player)
        self.assertEqual(relation.status, '订单已取消')
        self.assertIsNone(relation.room_join_deadline)
        player.refresh_from_db(); self.assertTrue(player.is_online)
        self.assertEqual(player.total_orders, 0)
        self.assertFalse(PlayerCancellationRecord.objects.exists())
        self.assertFalse(PlayerDiscipline.objects.filter(suspended_until__isnull=False).exists())
        state.refresh_from_db(); self.assertEqual(state.status, 'resolved')
        designation.refresh_from_db(); self.assertEqual(designation.status, 'cancelled')
        self.assertEqual(self.financial_snapshot(), before)
        with self.assertRaises(ValidationError):
            start_timer(order, player)

    def test_partial_lineup_can_cancel_without_financial_changes(self):
        from apps.orders.services import grab_order
        order, _, _ = self.make_dispatch()
        Order.objects.filter(pk=order.pk).update(required_players=2)
        order = grab_order(order.order_no, self.player())
        self.assertEqual(order.status, Order.STATUS_WAITING)
        before = self.financial_snapshot()
        response = self.cancel(order)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(self.financial_snapshot(), before)
        self.assertEqual(OrderPlayer.objects.get(order=order).status, '订单已取消')

    def test_started_or_completed_orders_are_rejected_without_changes(self):
        order, _, _ = self.make_dispatch()
        for status, started in [(Order.STATUS_IN_PROGRESS, None), (Order.STATUS_COMPLETED, None),
                                (Order.STATUS_READY_TO_START, timezone.now())]:
            with self.subTest(status=status, started=bool(started)):
                Order.objects.filter(pk=order.pk).update(status=status, timer_started_at=started)
                before = self.financial_snapshot(); logs = order.status_logs.count()
                response = self.cancel(order)
                self.assertEqual(response.status_code, 409, response.content[:300])
                order.refresh_from_db(); self.assertEqual(order.status, status)
                self.assertIsNone(order.canceled_at)
                self.assertEqual(order.status_logs.count(), logs)
                self.assertEqual(self.financial_snapshot(), before)

    def test_financial_anomaly_cannot_use_this_shortcut(self):
        from apps.earnings.models import PlayerEarning
        order, _, _ = self.make_dispatch()
        PlayerEarning.objects.create(order=order, player=self.player(), gross_amount=200,
            commission_rate=16, commission_amount=32, net_amount=168, review_until=timezone.now())
        before = self.financial_snapshot()
        response = self.cancel(order)
        self.assertEqual(response.status_code, 409, response.content[:300])
        self.assertEqual(self.financial_snapshot(), before)
        order.refresh_from_db(); self.assertEqual(order.status, Order.STATUS_WAITING)

    def test_no_receipt_or_wrong_source_cannot_be_cancelled_as_offline(self):
        from apps.dispatch.models import DispatchReceipt
        order, _, _ = self.make_dispatch()
        Order.objects.filter(pk=order.pk).update(source='self')
        self.assertEqual(self.cancel(order).status_code, 409)
        Order.objects.filter(pk=order.pk).update(source='staff')
        DispatchReceipt.objects.filter(order=order).delete()
        self.assertEqual(self.cancel(order).status_code, 409)
        order.refresh_from_db(); self.assertEqual(order.status, Order.STATUS_WAITING)

    def test_invalid_reason_and_extra_money_fields_are_rejected(self):
        order, _, _ = self.make_dispatch()
        for payload in [{}, {'reason': ''}, {'reason': '   '}, {'reason': '字'*201},
                        {'reason': '测试', 'refund': True}, {'reason': '测试', 'amount': '20'}]:
            with self.subTest(payload=payload):
                response = self.post('orders/'+order.order_no+'/cancel', payload)
                self.assertEqual(response.status_code, 400, response.content[:300])
        order.refresh_from_db(); self.assertEqual(order.status, Order.STATUS_WAITING)

    def test_endpoint_requires_permission_post_and_csrf(self):
        order, _, _ = self.make_dispatch(); url = '/dispatch/api/orders/'+order.order_no+'/cancel/'
        self.assertEqual(self.client.get(url).status_code, 405)
        guest = Client(); self.assertEqual(guest.post(url).status_code, 403)
        no_csrf = Client(enforce_csrf_checks=True); no_csrf.force_login(self.agent)
        self.assertEqual(no_csrf.post(url, data=json.dumps({'reason': '测试'}), content_type='application/json').status_code, 403)
        staff = get_user_model().objects.create_user(username='no-permission', is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.cancel(order).status_code, 403)
        order.refresh_from_db(); self.assertEqual(order.status, Order.STATUS_WAITING)
        staff.user_permissions.add(Permission.objects.get(content_type__app_label='dispatch', codename='use_console'))
        self.assertEqual(self.cancel(order).status_code, 200)

    def test_detail_reports_eligibility_and_cancellation_result(self):
        order, _, _ = self.make_dispatch(); url = '/dispatch/api/orders/'+order.order_no+'/'
        detail = self.client.get(url).json()
        self.assertTrue(detail.get('can_cancel_dispatch'), detail)
        self.cancel(order)
        detail = self.client.get(url).json()
        self.assertFalse(detail['can_cancel_dispatch'])
        self.assertEqual(detail['cancel_reason'], '老板临时取消')
        self.assertEqual(detail['offline_refund_status'], 'unconfirmed')
        self.assertIn('线下退款', detail['cancel_block_reason'])

    def test_not_found_is_clear(self):
        self.assertEqual(self.post('orders/not-found/cancel', {'reason': '测试'}).status_code, 404)

    def test_original_wallet_payment_and_refund_gate_stays_closed(self):
        from apps.wallet.order_spend import guard_order
        order, _, _ = self.make_dispatch()
        self.assertEqual(self.cancel(order).status_code, 200)
        order.refresh_from_db()
        for cancel in [False, True]:
            with self.assertRaises(ValidationError):
                guard_order(order, cancel=cancel)

    @override_settings(KOOK_ENABLED=True, KOOK_ORDER_EVENTS_ENABLED=True,
                       KOOK_PRODUCTION_ENABLED=True, KOOK_ORDER_NOTIFICATION_SCOPE='all',
                       KOOK_ORDER_PUBLIC_CREATED_SINCE='2020-01-01T00:00:00+08:00')
    def test_cancel_captures_kook_close_once_without_network(self):
        from apps.kook_integration.models import KookOutboxEvent
        order, _, _ = self.make_dispatch()
        first = self.cancel(order)
        self.assertEqual(first.status_code, 200, first.content[:300])
        self.assertEqual(self.cancel(order).status_code, 200)
        events = list(KookOutboxEvent.objects.filter(order_id=order.pk, event_type='order.cancelled'))
        self.assertEqual(len(events), 1)
        self.assertIn('已取消', events[0].payload['safe_snapshot']['status'])

    def test_audit_failure_rolls_back_whole_cancellation(self):
        order, _, _ = self.make_dispatch()
        from apps.dispatch.cancellations import cancel_dispatch
        with patch('apps.dispatch.cancellations.OrderStatusLog.objects.create', side_effect=RuntimeError('fixture audit unavailable')):
            with self.assertRaises(RuntimeError):
                cancel_dispatch(order.order_no, {'reason': '不能留下半个取消'}, self.agent)
        order.refresh_from_db(); self.assertEqual(order.status, Order.STATUS_WAITING)
        self.assertIsNone(order.canceled_at)
