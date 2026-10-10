"""Order-only canary verification; isolated settings and mocked transport only."""
from django.test import TestCase, override_settings
from apps.kook_integration.models import KookOutboxEvent, KookDelivery, KookOrderProjection
from . import test_kook_notifications as fixtures
from . import kook_notifications as bridge


@override_settings(KOOK_ENABLED=True, KOOK_ORDER_EVENTS_ENABLED=True,
                   KOOK_PRODUCTION_ENABLED=False, KOOK_TEST_ORDER_IDS=[],
                   KOOK_VERIFIED_CHANNEL_ID='offline-channel', KOOK_WECHAT_URL_LINK_ENABLED=False)
class NoLinkCanaryTests(TestCase):
    setUp = fixtures.BridgeTests.setUp
    order = fixtures.BridgeTests.order

    def test_capture_requires_explicit_order_allowlist_empty_is_closed(self):
        order = self.order()
        for ids in ([], [order.pk + 100]):
            with self.subTest(ids=ids), override_settings(KOOK_TEST_ORDER_IDS=ids):
                self.assertEqual(bridge.record_order_state(order.pk), [])
                self.assertFalse(KookOutboxEvent.objects.exists())
                self.assertFalse(KookDelivery.objects.exists())
                self.assertFalse(KookOrderProjection.objects.exists())
        with override_settings(KOOK_TEST_ORDER_IDS=[order.pk]):
            self.assertEqual(len(bridge.record_order_state(order.pk)), 1)
            self.assertEqual(bridge.record_order_state(order.pk), [])


    def send_flags(self, order):
        return override_settings(KOOK_SEND_ENABLED=True, KOOK_CHANNEL_SEND_ENABLED=True,
            KOOK_DM_SEND_ENABLED=True, KOOK_TARGET_CONFIG_VERSION='no-link',
            KOOK_VERIFIED_CONFIG_VERSION='no-link', KOOK_VERIFIED_BOT_KEY='offline-test-bot',
            KOOK_VERIFIED_GUILD_ID='offline-guild', KOOK_TEST_ORDER_IDS=[order.pk],
            KOOK_DM_ALLOWLIST=['offline-recipient'], KOOK_PLAYER_ALLOWLIST=[self.player.pk],
            KOOK_ORDER_DM_INVITATIONS_SINCE='2020-01-01T00:00:00+00:00',
            KOOK_ORDER_REVALIDATOR=bridge.revalidate_order)

    def bind(self):
        from apps.kook_integration.models import KookBinding
        from django.utils import timezone
        return KookBinding.objects.create(bot_key='offline-test-bot', player=self.player,
            kook_user_id='offline-recipient', notifications_enabled=True, verified_at=timezone.now())

    def test_order_gate_blocks_already_queued_channel_and_dm(self):
        from .designations import create_designations
        from apps.kook_integration.outbox import process_once
        from unittest.mock import Mock
        from apps.kook_integration.client import SendResult
        self.bind()
        order = self.order()
        client = Mock(bot_key='offline-test-bot')
        client.send.return_value = SendResult('sent', message_id='offline-message')
        with self.send_flags(order):
            create_designations(order, [self.player])
            self.assertEqual(set(KookDelivery.objects.values_list('scope', flat=True)), {'channel', 'dm'})
            with override_settings(KOOK_ORDER_EVENTS_ENABLED=False):
                self.assertEqual(process_once(client=client)['outbound'], 0)
                client.send.assert_not_called()
                self.assertEqual(set(KookDelivery.objects.values_list('last_error_code', flat=True)), {'ORDER_EVENTS_DISABLED'})


    def http_client(self):
        from unittest.mock import Mock
        from apps.kook_integration.client import KookClient
        transport = Mock()
        def post(url, **kwargs):
            payload = kwargs['json']
            message = payload.get('msg_id') or 'offline-msg-' + str(transport.post.call_count)
            return Mock(status_code=200, headers={}, json=lambda: {'code': 0, 'data': {'msg_id': message}})
        transport.post.side_effect = post
        return KookClient(token='offline-fake-token', transport=transport), transport

    def drain(self, client, transport, path, expected=1, contains=None):
        from apps.kook_integration.outbox import process_once
        from apps.kook_integration.models import KookEntryIntent
        before = transport.post.call_count
        self.assertEqual(process_once(client=client)['outbound'], expected)
        self.assertEqual(transport.post.call_count - before, expected)
        if expected:
            self.assertTrue(transport.post.call_args.args[0].endswith(path))
            content = transport.post.call_args.kwargs['json']['content']
            self.assertNotIn('(met)all(met)', content)
            self.assertNotIn('"click":"link"', content)
            self.assertNotIn('https://wxaurl', content)
            if contains:
                self.assertIn(contains, content)
        self.assertFalse(KookEntryIntent.objects.exists())
        self.assertEqual(process_once(client=client)['outbound'], 0)
        self.assertEqual(transport.post.call_count - before, expected)

    def create_public(self):
        from .boss_views import create_order_service
        return create_order_service({'boss_wechat':'offline-boss', 'package_id':self.package.pk,
            'required_players':2, 'quantity':1, 'boss_note':'通知测试，非客户订单'}, skip_content_security=True)

    def another_player(self, suffix):
        from django.contrib.auth.models import User
        from apps.accounts.models import ClientProfile
        from apps.players.models import Player
        user = User.objects.create_user(username='no-link-'+suffix)
        ClientProfile.objects.create(user=user, openid='no-link-'+suffix, nickname='no-link-'+suffix)
        return Player.objects.create(user=user, name=suffix, player_type=self.kind,
                                     status='approved', is_online=True)

    def test_public_service_create_grab_full_paid_cancel_replacement_and_fill(self):
        from apps.players.permission_views import grab_order_service
        from apps.payments.models import Payment
        from apps.payments.services import mark_payment_paid
        from .player_cancellations import cancel_player_order
        from .models import Order
        second, replacement = self.another_player('second'), self.another_player('replacement')
        order = self.create_public()
        self.assertFalse(KookOutboxEvent.objects.exists())  # unknown ID cannot auto-authorize
        client, http = self.http_client()
        with self.send_flags(order):
            bridge.record_order_state(order.pk)  # seed only this explicitly approved fresh test order
            self.drain(client, http, 'message/create', contains='状态：可接单')
            initial_content = http.post.call_args.kwargs['json']['content']
            self.assertIn('人数：0/2人', initial_content)
            self.assertIn('发布时间：', initial_content)
            self.assertIn('老板备注：', initial_content)
            self.assertIn('通知测试，非客户订单', initial_content)
            original = KookDelivery.objects.get().remote_message_id
            grab_order_service(order.order_no, self.player)
            self.drain(client, http, 'message/update', contains='状态：可接单')
            self.assertIn('人数：1/2人', http.post.call_args.kwargs['json']['content'])
            self.assertEqual(http.post.call_args.kwargs['json']['msg_id'], original)
            grab_order_service(order.order_no, second)
            self.drain(client, http, 'message/update', contains='状态：已接满 · 不可接单')
            self.assertIn('人数：2/2人', http.post.call_args.kwargs['json']['content'])
            self.assertEqual(http.post.call_args.kwargs['json']['msg_id'], original)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.STATUS_PENDING_PAYMENT)
            payment = Payment.objects.create(payment_no='NO-LINK-PUBLIC', order=order,
                channel='balance', scene='mini_program', amount=order.total_amount, status='paying')
            mark_payment_paid(payment)
            cancel_player_order(order.order_no, self.player, '隔离测试取消接单')
            self.drain(client, http, 'message/create')
            self.assertEqual(KookOrderProjection.objects.get(order=order).public_epoch, 1)
            replacement_message = KookDelivery.objects.get(event__event_type='replacement.opened').remote_message_id
            self.assertNotEqual(replacement_message, original)
            grab_order_service(order.order_no, replacement)
            self.drain(client, http, 'message/update', contains='状态：已接满 · 不可接单')
            self.assertEqual(http.post.call_args.kwargs['json']['msg_id'], replacement_message)
            self.assertEqual(KookOutboxEvent.objects.latest('occurred_at').event_type, 'replacement.resolved')
            self.assertEqual(order.order_players.count(), 2)

    def test_public_service_cancel_closes_existing_message(self):
        from .services import cancel_order
        order = self.create_public()
        client, http = self.http_client()
        with self.send_flags(order):
            bridge.record_order_state(order.pk)
            self.drain(client, http, 'message/create')
            message = KookDelivery.objects.get().remote_message_id
            cancel_order(order, '隔离通知测试取消')
            self.drain(client, http, 'message/update', contains='状态：已取消 · 不可接单')
            self.assertEqual(http.post.call_args.kwargs['json']['msg_id'], message)
            self.assertEqual(KookOutboxEvent.objects.latest('occurred_at').event_type, 'order.cancelled')

    def test_replacement_cancel_request_updates_same_message_without_claiming_order_cancelled(self):
        from .cancellation_models import OrderReplacementState
        from .replacements import request_cancel_remaining
        from .models import Order
        order = self.order(paid=True, status=Order.STATUS_IN_PROGRESS)
        OrderReplacementState.objects.create(order=order, mode='public', status='open', resume_status=order.status)
        client, http = self.http_client()
        with self.send_flags(order):
            bridge.record_order_state(order.pk)
            self.drain(client, http, 'message/create', contains='状态：可接单')
            message = KookDelivery.objects.get().remote_message_id
            request_cancel_remaining(order)
            self.drain(client, http, 'message/update', contains='状态：补位已结束 · 不可接单')
            self.assertNotIn('状态：已取消', http.post.call_args.kwargs['json']['content'])
            self.assertEqual(http.post.call_args.kwargs['json']['msg_id'], message)

    def test_reserved_slots_close_conservatively_not_full_or_replacement(self):
        from .designations import create_designations
        order = self.order(required_players=1)
        client, http = self.http_client()
        with self.send_flags(order):
            bridge.record_order_state(order.pk)
            self.drain(client, http, 'message/create')
            message = KookDelivery.objects.get().remote_message_id
            create_designations(order, [self.player])
            self.drain(client, http, 'message/update', contains='状态：已关闭 · 不可接单')
            self.assertEqual(order.order_players.count(), 0)
            self.assertNotIn('已接满', http.post.call_args.kwargs['json']['content'])
            self.assertNotIn('补位已结束', http.post.call_args.kwargs['json']['content'])
            self.assertEqual(http.post.call_args.kwargs['json']['msg_id'], message)

    def test_full_then_cancel_updates_current_card_even_after_round_closed(self):
        from apps.players.permission_views import grab_order_service
        from .services import cancel_order
        order = self.order(required_players=1)
        client, http = self.http_client()
        with self.send_flags(order):
            bridge.record_order_state(order.pk)
            self.drain(client, http, 'message/create')
            message = KookDelivery.objects.get().remote_message_id
            grab_order_service(order.order_no, self.player)
            self.drain(client, http, 'message/update', contains='状态：已接满')
            order.refresh_from_db()
            cancel_order(order, 'isolated cancellation after full')
            self.drain(client, http, 'message/update', contains='状态：已取消 · 不可接单')
            self.assertEqual(http.post.call_args.kwargs['json']['msg_id'], message)

    def test_cancel_pending_invitation_updates_same_dm(self):
        from .designations import create_designations
        from .services import cancel_order
        self.bind()
        order = self.order(required_players=1)
        client, http = self.http_client()
        with self.send_flags(order):
            create_designations(order, [self.player])
            self.drain(client, http, 'direct-message/create')
            message = KookDelivery.objects.get().remote_message_id
            cancel_order(order, 'isolated invitation cancellation')
            self.drain(client, http, 'direct-message/update', contains='状态：邀请已取消')
            self.assertEqual(http.post.call_args.kwargs['json']['msg_id'], message)

    def test_delayed_old_epoch_full_close_uses_frozen_facts_after_real_reopen(self):
        from apps.players.permission_views import grab_order_service
        from .player_cancellations import cancel_player_order
        from apps.kook_integration.outbox import process_once
        order = self.order(paid=True)
        second = self.another_player('epoch-second')
        client, http = self.http_client()
        with self.send_flags(order):
            bridge.record_order_state(order.pk)
            self.drain(client, http, 'message/create')
            original = KookDelivery.objects.get().remote_message_id
            grab_order_service(order.order_no, self.player)
            self.drain(client, http, 'message/update')
            grab_order_service(order.order_no, second)  # close queued, not sent yet
            cancel_player_order(order.order_no, self.player, 'isolated epoch test')
            self.assertEqual(KookOrderProjection.objects.get(order=order).public_epoch, 1)
            self.assertEqual(process_once(client=client)['outbound'], 2)
            old, new = http.post.call_args_list[-2:]
            self.assertTrue(old.args[0].endswith('message/update'))
            self.assertEqual(old.kwargs['json']['msg_id'], original)
            self.assertIn('状态：已接满 · 不可接单', old.kwargs['json']['content'])
            self.assertIn('人数：2/2人', old.kwargs['json']['content'])
            self.assertTrue(new.args[0].endswith('message/create'))
            self.assertIn('状态：可接单', new.kwargs['json']['content'])
            self.assertIn('人数：1/2人', new.kwargs['json']['content'])
            for call in (old, new):
                self.assertNotIn('(met)all(met)', call.kwargs['json']['content'])
            self.assertEqual(process_once(client=client)['outbound'], 0)

    def test_legacy_terminal_payload_cannot_claim_current_active_or_unknown_reason(self):
        from types import SimpleNamespace
        from apps.kook_integration.rendering import render
        for kind in ('public_slots.closed', 'replacement.resolved'):
            for raw in ('待接单', '进行中', '本轮已关闭 · 不可接单（补位已结束）', ''):
                event = SimpleNamespace(event_type=kind, payload={
                    'audience': 'public_channel', 'safe_snapshot': {'status': raw}})
                content = render(event)
                self.assertIn('状态：已关闭 · 不可接单', content)
                self.assertNotIn('状态：已接满', content)
                self.assertNotIn('状态：补位已结束', content)

    def test_invitation_inactive_without_terminal_row_fact_stays_conservative(self):
        from .designations import create_designations
        from .models import Order
        self.bind()
        order = self.order(required_players=1)
        client, http = self.http_client()
        with self.send_flags(order):
            create_designations(order, [self.player])
            self.drain(client, http, 'direct-message/create')
            message = KookDelivery.objects.get().remote_message_id
            order.status = Order.STATUS_COMPLETED  # isolated characterization, no guessed response
            order.save(update_fields=['status'])
            bridge.record_order_state(order.pk)
            self.drain(client, http, 'direct-message/update', contains='状态：邀请已关闭')
            self.assertEqual(order.designations.get().status, 'pending')
            self.assertEqual(http.post.call_args.kwargs['json']['msg_id'], message)

    def targeted_flow(self, outcome):
        from .boss_views import create_order_service
        from .designations import accept_designation, decline_designation, expire_due_designations
        from apps.catalog.models import Package
        from apps.payments.models import Payment
        from apps.payments.services import mark_payment_paid
        from apps.wallet.models import ClientWallet
        from django.utils import timezone
        from datetime import timedelta
        from unittest.mock import patch
        self.bind()
        ClientWallet.objects.get_or_create(profile=self.player.user.client_profile)
        package = Package.objects.create(name='offline-targeted', base_price=15, player_count=1,
            selling_mode=Package.SELLING_MODE_PLAYER_DESIGNATED, owner_player=self.player)
        order = create_order_service({'boss_wechat':'offline-boss','package_id':package.pk,
            'quantity':1, 'boss_note':'指定私信隔离测试'}, user=self.player.user, skip_content_security=True)
        self.assertEqual(order.fulfillment_mode, 'targeted')
        self.assertFalse(order.designations.exists())
        client, http = self.http_client()
        with self.send_flags(order):
            bridge.record_order_state(order.pk)
            self.assertFalse(KookOutboxEvent.objects.exists())
            payment = Payment.objects.create(payment_no='NO-LINK-TARGETED', order=order,
                channel='balance', scene='mini_program', amount=order.total_amount, status='paying')
            with patch('apps.orders.targeted_notifications.notify_paid_targeted_order'):
                with self.captureOnCommitCallbacks(execute=True):
                    mark_payment_paid(payment)
                    mark_payment_paid(payment)
            self.assertEqual(KookOutboxEvent.objects.filter(event_type='designation.created').count(), 1)
            self.drain(client, http, 'direct-message/create', contains='待接受指定')
            message = KookDelivery.objects.get().remote_message_id
            if outcome == 'accept':
                accept_designation(order.order_no, self.player)
            elif outcome == 'decline':
                decline_designation(order.order_no, self.player)
            else:
                row = order.designations.get()
                row.expires_at = timezone.now() - timedelta(seconds=1)
                row.save(update_fields=['expires_at'])
                expire_due_designations(order=order)
            label = {'accept': '已接受', 'decline': '已拒绝', 'expire': '已过期'}[outcome]
            self.drain(client, http, 'direct-message/update', contains='状态：邀请' + label)
            self.assertEqual(http.post.call_args.kwargs['json']['msg_id'], message)
            self.assertFalse(KookOutboxEvent.objects.filter(payload__audience='public_channel').exists())
            self.assertEqual(order.designations.get().status, {'accept':'accepted', 'decline':'declined', 'expire':'expired'}[outcome])

    def test_targeted_paid_accept_updates_same_dm(self):
        self.targeted_flow('accept')

    def test_targeted_paid_decline_updates_same_dm(self):
        self.targeted_flow('decline')

    def test_targeted_paid_expire_updates_same_dm(self):
        self.targeted_flow('expire')


    def test_all_order_delivery_scopes_and_operations_fail_closed_after_gate_changes(self):
        from .designations import create_designations
        from apps.kook_integration.outbox import fresh
        self.bind()
        order = self.order()
        with self.send_flags(order):
            create_designations(order, [self.player])
            for delivery in KookDelivery.objects.select_related('event', 'stream'):
                for operation in ('create', 'update'):
                    delivery.operation = operation
                    with self.subTest(scope=delivery.scope, operation=operation):
                        self.assertEqual(fresh(delivery), (True, ''))
                        for flags, reason in [
                            ({'KOOK_TEST_ORDER_IDS': []}, 'ORDER_NOT_ALLOWLISTED'),
                            ({'KOOK_TEST_ORDER_IDS': [order.pk + 100]}, 'ORDER_NOT_ALLOWLISTED'),
                            ({'KOOK_ORDER_EVENTS_ENABLED': False}, 'ORDER_EVENTS_DISABLED'),
                            ({'KOOK_SEND_ENABLED': False}, 'SEND_DISABLED'),
                        ]:
                            with self.subTest(flags=flags), override_settings(**flags):
                                self.assertEqual(fresh(delivery), (False, reason))
                        scope_flags = ([{'KOOK_DM_ALLOWLIST': []}, {'KOOK_PLAYER_ALLOWLIST': []},
                                        {'KOOK_DM_SEND_ENABLED': False}]
                                       if delivery.scope == 'dm' else
                                       [{'KOOK_VERIFIED_CHANNEL_ID': 'other'}, {'KOOK_CHANNEL_SEND_ENABLED': False},
                                        {'KOOK_VERIFIED_BOT_KEY': 'other'}, {'KOOK_VERIFIED_CONFIG_VERSION': 'other'}])
                        for flags in scope_flags:
                            with self.subTest(flags=flags), override_settings(**flags):
                                self.assertEqual(fresh(delivery), (False, 'SEND_DISABLED'))

    def test_order_gate_rechecked_after_claim_before_http(self):
        from apps.kook_integration import outbox
        from django.conf import settings
        from unittest.mock import patch
        order = self.order()
        client, http = self.http_client()
        with self.send_flags(order), override_settings(KOOK_ORDER_EVENTS_ENABLED=True):
            bridge.record_order_state(order.pk)
            original = outbox.claim_delivery
            def claim(pk):
                result = original(pk)
                settings.KOOK_ORDER_EVENTS_ENABLED = False
                return result
            with patch.object(outbox, 'claim_delivery', side_effect=claim):
                self.assertEqual(outbox.process_once(client=client)['outbound'], 0)
            http.post.assert_not_called()
            self.assertEqual(KookDelivery.objects.get().last_error_code, 'ORDER_EVENTS_DISABLED')

    def test_test_dm_remains_available_without_order_gate_or_order_ids(self):
        import uuid
        from apps.kook_integration.outbox import enqueue_test
        self.bind()
        order = self.order()
        client, http = self.http_client()
        with self.send_flags(order), override_settings(KOOK_ORDER_EVENTS_ENABLED=False, KOOK_TEST_ORDER_IDS=[]):
            enqueue_test(self.player, uuid.uuid4(), '127.0.0.1')
            self.drain(client, http, 'direct-message/create')
            self.assertIsNone(KookOutboxEvent.objects.get().order_id)
