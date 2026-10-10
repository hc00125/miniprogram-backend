"""Offline bridge tests; real ORM/outbox, no external transport."""
from datetime import timedelta
from django.test import TestCase, override_settings
from django.db import transaction
from django.utils import timezone
from django.contrib.auth.models import User
from apps.catalog.models import Package, PlayerType
from apps.players.models import Player
from apps.accounts.models import ClientProfile
from apps.kook_integration.models import KookOutboxEvent, KookDelivery, KookOrderProjection
from .models import Order, OrderDesignation, OrderPlayer


@override_settings(KOOK_ENABLED=True, KOOK_ORDER_EVENTS_ENABLED=True, KOOK_PRODUCTION_ENABLED=True, KOOK_VERIFIED_CHANNEL_ID='offline-channel')
class BridgeTests(TestCase):
    def setUp(self):
        self.package = Package.objects.create(name='private-package', base_price=15, player_count=2)
        self.kind = PlayerType.objects.create(name='娱乐陪', priority=1)
        user = User.objects.create_user(username='offline-player')
        ClientProfile.objects.create(user=user, openid='offline-openid', nickname='offline-nickname')
        self.player = Player.objects.create(user=user, name='offline-player', player_type=self.kind, status='approved', is_online=True)

    def authorize_dm(self):
        """Explicit all-scope fixture consent, dual allowlists and fixed cutoff."""
        from apps.kook_integration.models import KookBinding
        flags = override_settings(KOOK_SEND_ENABLED=True, KOOK_DM_SEND_ENABLED=True,
            KOOK_DM_ALLOWLIST=['offline-recipient'], KOOK_PLAYER_ALLOWLIST=[self.player.pk],
            KOOK_ORDER_DM_INVITATIONS_SINCE='2020-01-01T00:00:00+00:00')
        flags.enable()
        self.addCleanup(flags.disable)
        return KookBinding.objects.create(bot_key='offline-test-bot', player=self.player,
            kook_user_id='offline-recipient', notifications_enabled=True, verified_at=timezone.now())

    def order(self, **changes):
        data = dict(order_no='OFFLINE001', package=self.package, required_players=2, boss_wechat='NEVER_PUBLIC', boss_note='规格：系统\n联系我 13800138000\n', total_amount=15)
        data.update(changes)
        return Order.objects.create(**data)

    def test_public_snapshot_is_durable_idempotent_and_private(self):
        from . import kook_notifications as bridge
        order = self.order()
        with transaction.atomic():
            events = bridge.record_order_state(order.pk)
            self.assertEqual(len(events), 1)
            self.assertEqual(bridge.record_order_state(order.pk), [])
        event = KookOutboxEvent.objects.get()
        self.assertEqual(event.event_type, 'public_slots.opened')
        self.assertEqual(event.payload['safe_snapshot']['boss_note'], '联系我 13800138000')
        self.assertNotIn('private-package', str(event.payload))
        self.assertNotIn('NEVER_PUBLIC', str(event.payload))
        self.assertEqual(KookDelivery.objects.count(), 1)

    def test_invitation_lifecycle_actual_entries_and_full_close(self):
        self.authorize_dm()
        from . import kook_notifications as bridge
        from .designations import create_designations, decline_designation, accept_designation
        order = self.order(required_players=1)
        with transaction.atomic():
            create_designations(order, [self.player])
        self.assertEqual(list(KookOutboxEvent.objects.values_list('event_type', flat=True)), ['designation.created'])
        decline_designation(order.order_no, self.player)
        self.assertTrue(KookOutboxEvent.objects.filter(event_type='designation.closed').exists())
        self.assertTrue(KookOutboxEvent.objects.filter(event_type='public_slots.opened').exists())
        order.designations.all().delete()
        with transaction.atomic():
            create_designations(order, [self.player])
        accept_designation(order.order_no, self.player)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PENDING_PAYMENT)
        self.assertFalse(bridge.public_open(order))
        self.assertEqual(KookOutboxEvent.objects.filter(designation_id__isnull=False).latest('occurred_at').event_type, 'designation.closed')

    def test_targeted_unpaid_no_events_paid_invitation_and_rollback(self):
        self.authorize_dm()
        from . import kook_notifications as bridge
        order = self.order(fulfillment_mode='targeted', target_player=self.player)
        OrderDesignation.objects.create(order=order, player=self.player, expires_at=timezone.now()+timedelta(minutes=10))
        bridge.record_order_state(order.pk)
        self.assertFalse(KookOutboxEvent.objects.exists())
        try:
            with transaction.atomic():
                order.paid = True
                order.save(update_fields=['paid'])
                bridge.record_order_state(order.pk)
                self.assertTrue(KookOutboxEvent.objects.filter(event_type='designation.created').exists())
                raise RuntimeError('rollback')
        except RuntimeError:
            pass
        self.assertFalse(KookOutboxEvent.objects.exists())

    def test_navigation_is_read_only_current_permissions_and_owner(self):
        from . import kook_notifications as bridge
        from apps.kook_integration.navigation import create_intent, resolve_intent
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        order = self.order()
        token, intent = create_intent(order.pk, 'channel', timezone.now()+timedelta(minutes=20))
        with override_settings(KOOK_ENTRY_RESOLVER=bridge.resolve_entry):
            with CaptureQueriesContext(connection) as queries:
                result = resolve_intent(token, self.player)
            self.assertEqual(result['state'], 'available')
            self.assertEqual(result['safe_target'], 'pages/player/grab/index')
            self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
            self.player.can_accept_orders = False
            self.player.save(update_fields=['can_accept_orders'])
            self.assertEqual(resolve_intent(token, self.player)['state'], 'forbidden')
            self.assertNotIn('order_no', resolve_intent(token, self.player))

    def test_revalidator_rejects_stale_open_but_allows_close_update(self):
        from . import kook_notifications as bridge
        order = self.order()
        bridge.record_order_state(order.pk)
        delivery = KookDelivery.objects.get()
        self.assertTrue(bridge.revalidate_order(delivery.event.payload, delivery))
        order.status = Order.STATUS_CANCELLED
        order.save(update_fields=['status'])
        self.assertFalse(bridge.revalidate_order(delivery.event.payload, delivery))
        bridge.record_order_state(order.pk)
        close = KookDelivery.objects.exclude(pk=delivery.pk).get()
        self.assertFalse(bridge.revalidate_order(close.event.payload, close))
        from apps.kook_integration.models import KookMessageProjection
        KookMessageProjection.objects.filter(pk=close.stream_id).update(remote_message_id='offline-sent')
        self.assertTrue(bridge.revalidate_order(close.event.payload, close))

    def test_runtime_create_grab_cancel_and_disabled_switch(self):
        from . import boss_views, services
        from apps.players import permission_views
        from .duration_services import create_order
        self.assertIs(boss_views.create_order_service, create_order)
        self.assertIs(permission_views.grab_order_service, services.grab_order)
        data = {'boss_wechat':'offline-boss', 'package_id':self.package.pk, 'required_players':2,
                'quantity':1, 'boss_note':'备注 13800138000'}
        order = boss_views.create_order_service(data, skip_content_security=True)
        self.assertEqual(KookOutboxEvent.objects.filter(order_id=order.pk).count(), 1)
        # Simulate a prior persisted successful send; transport is never called.
        from apps.kook_integration.models import KookMessageProjection
        KookMessageProjection.objects.filter(order_id=order.pk).update(remote_message_id='offline-sent-id')
        permission_views.grab_order_service(order.order_no, self.player)
        self.assertTrue(KookOutboxEvent.objects.filter(event_type='public_slots.changed').exists())
        services.cancel_order(order, 'offline-cancel')
        self.assertTrue(KookOutboxEvent.objects.filter(event_type='order.cancelled').exists())
        before = KookOutboxEvent.objects.count()
        with override_settings(KOOK_ORDER_EVENTS_ENABLED=False):
            services.create_order(data)
        self.assertEqual(KookOutboxEvent.objects.count(), before)

    def test_runtime_batch_expiry_releases_reserved_slot(self):
        self.authorize_dm()
        from .designations import create_designations, expire_due_designations
        order = self.order(required_players=1)
        create_designations(order, [self.player])
        row = order.designations.get()
        row.expires_at = timezone.now()-timedelta(seconds=1)
        row.save(update_fields=['expires_at'])
        expire_due_designations()
        row.refresh_from_db()
        self.assertEqual(row.status, 'expired')
        self.assertTrue(KookOutboxEvent.objects.filter(event_type='designation.closed').exists())
        self.assertTrue(KookOutboxEvent.objects.filter(event_type='public_slots.opened').exists())

    def test_runtime_paid_replacement_with_unchanged_order_status(self):
        from .cancellation_models import OrderReplacementState
        from .replacements import publish_replacement_public
        from apps.players.permission_views import grab_order_service
        order = self.order(paid=True, status=Order.STATUS_IN_PROGRESS)
        OrderReplacementState.objects.create(order=order, mode='targeted', status='open', resume_status=Order.STATUS_IN_PROGRESS)
        publish_replacement_public(order)
        self.assertTrue(KookOutboxEvent.objects.filter(order_id=order.pk).exists())
        before = KookOutboxEvent.objects.count()
        grab_order_service(order.order_no, self.player)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_IN_PROGRESS)
        self.assertGreater(KookOutboxEvent.objects.count(), before)

    def test_public_replacement_events_close_only_update_and_types(self):
        from . import kook_notifications as bridge
        from .cancellation_models import OrderReplacementState
        order = self.order(paid=True, status=Order.STATUS_IN_PROGRESS, required_players=1)
        OrderReplacementState.objects.create(order=order, mode='public', status='open', resume_status=Order.STATUS_IN_PROGRESS)
        bridge.record_order_state(order.pk)
        self.assertEqual(KookOutboxEvent.objects.get().event_type, 'replacement.opened')
        from apps.players.permission_views import grab_order_service
        grab_order_service(order.order_no, self.player)
        event = KookOutboxEvent.objects.latest('occurred_at')
        self.assertEqual(event.event_type, 'replacement.resolved')
        self.assertEqual(KookDelivery.objects.get(event=event).operation, 'update')

    def test_boss_paid_cancellation_effective_view_hook(self):
        from . import kook_notifications as bridge, boss_views
        from rest_framework.test import APIRequestFactory, force_authenticate
        order = self.order(boss_user=self.player.user, paid=True)
        bridge.record_order_state(order.pk)
        request = APIRequestFactory().post('/', {'reason':'offline cancel'}, format='json')
        force_authenticate(request, user=self.player.user)
        response = boss_views.cancel_order(request, order.order_no)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(KookOutboxEvent.objects.filter(event_type='order.cancelled').exists())

    def test_navigation_dm_owner_and_revalidation_expiry(self):
        from . import kook_notifications as bridge
        from apps.kook_integration.models import KookBinding
        from apps.kook_integration.navigation import create_intent, resolve_intent
        from .designations import create_designations
        binding = self.authorize_dm()
        order = self.order()
        create_designations(order, [self.player])
        row = order.designations.get()
        token, intent = create_intent(order.pk, 'dm', row.expires_at, designation_id=row.pk, binding=binding)
        with override_settings(KOOK_ENTRY_RESOLVER=bridge.resolve_entry):
            self.assertEqual(resolve_intent(token, self.player)['state'], 'invited')
            other = Player.objects.create(name='offline-other', player_type=self.kind, status='approved')
            self.assertEqual(resolve_intent(token, other)['state'], 'forbidden')
            self.assertNotIn('order_no', resolve_intent(token, other))
            delivery = KookDelivery.objects.get(scope='dm')
            self.assertTrue(bridge.revalidate_order(delivery.event.payload, delivery))
            row.expires_at = timezone.now()-timedelta(seconds=1)
            row.save(update_fields=['expires_at'])
            self.assertFalse(bridge.revalidate_order(delivery.event.payload, delivery))
            self.assertEqual(resolve_intent(token, self.player)['state'], 'closed')

    def test_real_paid_callback_duplicate_and_targeted_refund_no_public_leak(self):
        self.authorize_dm()
        from unittest.mock import patch
        from apps.payments.models import Payment
        from apps.payments.services import mark_payment_paid
        from .designations import decline_designation
        from apps.wallet.models import ClientWallet
        ClientWallet.objects.get_or_create(profile=self.player.user.client_profile)
        order = self.order(boss_user=self.player.user, fulfillment_mode='targeted', target_player=self.player,
                           status=Order.STATUS_PENDING_PAYMENT, required_players=1)
        payment = Payment.objects.create(payment_no='OFFLINE-PAY', order=order, channel='balance', scene='mini_program', amount=15, status='paying')
        with patch('apps.orders.targeted_notifications.notify_paid_targeted_order') as old_notify:
            with self.captureOnCommitCallbacks(execute=True):
                mark_payment_paid(payment)
                mark_payment_paid(payment)
            self.assertEqual(old_notify.call_count, 1)
        self.assertEqual(KookOutboxEvent.objects.filter(event_type='designation.created').count(), 1)
        decline_designation(order.order_no, self.player)
        self.assertFalse(KookOutboxEvent.objects.filter(payload__audience='public_channel').exists())
        self.assertTrue(KookOutboxEvent.objects.filter(event_type='designation.closed').exists())
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)

    def test_closed_invitation_remains_revalidatable_after_unrelated_public_change(self):
        from . import kook_notifications as bridge
        from .designations import create_designations, decline_designation
        from apps.kook_integration.models import KookBinding
        self.authorize_dm()
        order = self.order()
        create_designations(order, [self.player])
        decline_designation(order.order_no, self.player)
        close = KookDelivery.objects.get(scope='dm', event__event_type='designation.closed')
        order.boss_note = '新的人工备注'
        order.save(update_fields=['boss_note'])
        bridge.record_order_state(order.pk)
        self.assertTrue(bridge.revalidate_order(close.event.payload, close))

    def test_final_public_slot_closes_and_rollback_rolls_back_business(self):
        from . import kook_notifications as bridge
        from apps.players.permission_views import grab_order_service
        order = self.order(required_players=1)
        bridge.record_order_state(order.pk)
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                grab_order_service(order.order_no, self.player)
                self.assertTrue(KookOutboxEvent.objects.filter(event_type='public_slots.closed').exists())
                raise RuntimeError('abort caller transaction')
        self.assertFalse(order.order_players.exists())
        self.assertFalse(KookOutboxEvent.objects.filter(event_type='public_slots.closed').exists())
        grab_order_service(order.order_no, self.player)
        self.assertTrue(KookOutboxEvent.objects.filter(event_type='public_slots.closed').exists())


    def test_reopen_uses_persisted_epoch_not_revision(self):
        from . import kook_notifications as bridge
        from apps.kook_integration.models import KookMessageProjection
        order = self.order(required_players=1)
        bridge.record_order_state(order.pk)
        initial = KookOrderProjection.objects.get(order=order).public_epoch
        KookMessageProjection.objects.filter(order_id=order.pk).update(remote_message_id='offline-old')
        order.order_players.create(player=self.player)
        bridge.record_order_state(order.pk)
        order.order_players.all().delete()
        bridge.record_order_state(order.pk)
        latest = KookOutboxEvent.objects.latest('occurred_at')
        self.assertEqual(latest.payload['public_epoch'], initial + 1)
        self.assertEqual(KookDelivery.objects.get(event=latest).operation, 'create')
        order.boss_note = '同轮改备注'
        order.save(update_fields=['boss_note'])
        bridge.record_order_state(order.pk)
        self.assertEqual(KookOrderProjection.objects.get(order=order).public_epoch, initial + 1)
        self.assertEqual(bridge.record_order_state(order.pk), [])

    def test_replacement_first_publish_mentions_but_same_round_update_does_not(self):
        from unittest.mock import Mock
        from apps.kook_integration.outbox import process_once
        from apps.kook_integration.client import SendResult
        from . import kook_notifications as bridge
        from .cancellation_models import OrderReplacementState
        order = self.order(paid=True, status=Order.STATUS_IN_PROGRESS, required_players=1)
        OrderReplacementState.objects.create(order=order, mode='public', status='open', resume_status=Order.STATUS_IN_PROGRESS)
        bridge.record_order_state(order.pk)
        client = Mock(bot_key='offline-test-bot')
        client.send.return_value = SendResult('sent', message_id='offline-first')
        with override_settings(KOOK_SEND_ENABLED=True, KOOK_CHANNEL_SEND_ENABLED=True,
                KOOK_TARGET_CONFIG_VERSION='v1', KOOK_VERIFIED_CONFIG_VERSION='v1',
                KOOK_VERIFIED_BOT_KEY='offline-test-bot', KOOK_VERIFIED_GUILD_ID='offline-guild',
                KOOK_PRODUCTION_ENABLED=True, KOOK_MENTION_ALL_VERIFIED=True,
                KOOK_MENTION_FIRST_PUBLIC_ENABLED=True, KOOK_MENTION_REOPEN_ENABLED=True,
                KOOK_ORDER_REVALIDATOR=bridge.revalidate_order):
            self.assertEqual(process_once(client=client)['outbound'], 1)
            self.assertIn('(met)all(met)', client.send.call_args.args[2])
            order.boss_note = '同轮修改'
            order.save(update_fields=['boss_note'])
            bridge.record_order_state(order.pk)
            self.assertEqual(process_once(client=client)['outbound'], 1)
            self.assertNotIn('(met)all(met)', client.send.call_args.args[2])
            self.assertEqual(client.send.call_args.kwargs['operation'], 'update')

    def test_worker_creates_navigation_link_and_degrades_without_credentials(self):
        import json
        from unittest.mock import Mock, patch
        from apps.kook_integration.outbox import process_once
        from apps.kook_integration.client import SendResult
        from apps.kook_integration.models import KookEntryIntent
        from apps.kook_integration.navigation import resolve_intent
        from . import kook_notifications as bridge
        order = self.order()
        bridge.record_order_state(order.pk)
        transport = Mock()
        transport.post.return_value = Mock(status_code=200, json=lambda: {'errcode':0, 'url_link':'https://wxaurl.cn/offline'})
        client = Mock(bot_key='offline-test-bot')
        client.send.return_value = SendResult('sent', message_id='offline-linked')
        flags = dict(KOOK_SEND_ENABLED=True, KOOK_CHANNEL_SEND_ENABLED=True,
            KOOK_TARGET_CONFIG_VERSION='v1', KOOK_VERIFIED_CONFIG_VERSION='v1',
            KOOK_VERIFIED_BOT_KEY='offline-test-bot', KOOK_VERIFIED_GUILD_ID='offline-guild',
            KOOK_TEST_ORDER_IDS=[order.pk], KOOK_ORDER_REVALIDATOR=bridge.revalidate_order,
            KOOK_ENTRY_RESOLVER=bridge.resolve_entry, KOOK_WECHAT_URL_LINK_ENABLED=True,
            KOOK_TEST_SECRETS={'wechat_access_token':'offline-token'})
        with override_settings(**flags), patch('apps.kook_integration.wechat_token.get_access_token', return_value='offline-token'), patch('requests.Session', return_value=transport):
            self.assertEqual(process_once(client=client)['outbound'], 1)
            content = client.send.call_args.args[2]
            self.assertIn('https://wxaurl.cn/offline', content)
            self.assertNotIn('offline-token', content)
            modules = json.loads(content)[0]['modules']
            self.assertEqual(modules[-1]['elements'][0]['click'], 'link')
            token = transport.post.call_args.kwargs['json']['query'].split('=',1)[1]
            self.assertEqual(resolve_intent(token, self.player)['state'], 'available')
            self.assertEqual(KookEntryIntent.objects.count(), 1)
            order.boss_note = '更新'
            order.save(update_fields=['boss_note'])
            bridge.record_order_state(order.pk)
            process_once(client=client)
            self.assertEqual(transport.post.call_count, 1)  # cached opaque link
            KookEntryIntent.objects.update(revoked_at=timezone.now())
            order.boss_note = '再次更新'
            order.save(update_fields=['boss_note'])
            bridge.record_order_state(order.pk)
            transport.post.side_effect = RuntimeError('credential-bearing HTTP error')
            process_once(client=client)
            self.assertNotIn('"click":"link"', client.send.call_args.args[2])
            self.assertIn('通知链接暂不可用', client.send.call_args.args[2])

    def test_player_cancel_entry_reopens_round_and_replacement_request_revoke(self):
        from . import kook_notifications as bridge
        from .player_cancellations import cancel_player_order
        from .replacements import request_cancel_remaining, revoke_cancel_remaining
        order = self.order(paid=True, required_players=1)
        bridge.record_order_state(order.pk)
        OrderPlayer.objects.create(order=order, player=self.player)
        order.status = Order.STATUS_READY_TO_START
        order.save(update_fields=['status'])
        bridge.record_order_state(order.pk)
        cancel_player_order(order.order_no, self.player, '离线测试退出')
        event = KookOutboxEvent.objects.latest('occurred_at')
        self.assertEqual(event.event_type, 'replacement.opened')
        self.assertEqual(event.payload['public_epoch'], 1)
        order.refresh_from_db()
        request_cancel_remaining(order)
        self.assertEqual(KookOutboxEvent.objects.latest('occurred_at').event_type, 'replacement.resolved')
        revoke_cancel_remaining(order)
        self.assertEqual(KookOutboxEvent.objects.latest('occurred_at').event_type, 'public_slots.changed')

    def test_replacement_reassign_decline_expire_accept_entries(self):
        self.authorize_dm()
        from .replacements import reassign_replacement, decline_replacement_designation, accept_replacement_designation
        from .designations import expire_due_designations
        from .cancellation_models import OrderReplacementState
        order = self.order(paid=True, status=Order.STATUS_IN_PROGRESS, required_players=1)
        OrderReplacementState.objects.create(order=order, mode='targeted', status='open', resume_status=Order.STATUS_IN_PROGRESS)
        row = reassign_replacement(order, self.player.pk)
        self.assertTrue(KookOutboxEvent.objects.filter(event_type='designation.created', designation_id=row.pk).exists())
        decline_replacement_designation(order.order_no, self.player)
        self.assertEqual(KookOutboxEvent.objects.filter(designation_id=row.pk).latest('occurred_at').event_type, 'designation.closed')
        row = reassign_replacement(order, self.player.pk)
        row.expires_at = timezone.now()-timedelta(seconds=1)
        row.save(update_fields=['expires_at'])
        expire_due_designations()
        self.assertEqual(KookOutboxEvent.objects.filter(designation_id=row.pk).latest('occurred_at').event_type, 'designation.closed')
        reassign_replacement(order, self.player.pk)
        accept_replacement_designation(order.order_no, self.player)
        self.assertEqual(KookOutboxEvent.objects.filter(designation_id=row.pk).latest('occurred_at').event_type, 'designation.closed')
        self.assertTrue(order.order_players.filter(player=self.player).exists())

    def test_pending_first_public_revision_still_mentions_once(self):
        from . import kook_notifications as bridge
        order = self.order()
        bridge.record_order_state(order.pk)
        OrderPlayer.objects.create(order=order, player=self.player)
        bridge.record_order_state(order.pk)
        # A worker supersedes revision 1: revision 2 is still the initial publish.
        self.assertEqual(KookOutboxEvent.objects.latest('occurred_at').event_type, 'public_slots.opened')

    def test_admin_save_related_records_final_inline_facts_and_rolls_back(self):
        from django.contrib.admin.sites import AdminSite
        from django.forms import modelform_factory, inlineformset_factory
        from django.test import RequestFactory
        from .admin import OrderAdmin
        admin = OrderAdmin(Order, AdminSite())
        request = RequestFactory().post('/')
        Form = modelform_factory(Order, fields=['order_no', 'package', 'required_players', 'boss_note', 'status'])
        Inline = inlineformset_factory(Order, OrderPlayer, fields=['player'], extra=1)
        form = Form(data={'order_no':'ADMIN-OFFLINE', 'package':self.package.pk,
                          'required_players':2, 'boss_note':'final admin note', 'status':Order.STATUS_WAITING})
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save(commit=False)
        with transaction.atomic():
            admin.save_model(request, obj, form, False)
            self.assertFalse(KookOutboxEvent.objects.exists())
            inline = Inline(data={'order_players-TOTAL_FORMS':1, 'order_players-INITIAL_FORMS':0,
                'order_players-0-player':self.player.pk}, instance=obj, prefix='order_players')
            self.assertTrue(inline.is_valid(), inline.errors)
            admin.save_related(request, form, [inline], False)
            self.assertEqual(KookOutboxEvent.objects.count(), 1)
            self.assertEqual(KookOutboxEvent.objects.get().payload['safe_snapshot']['participant_count'], 1)
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                form = Form(data={'order_no':obj.order_no, 'package':self.package.pk,
                    'required_players':2, 'boss_note':'cancel note', 'status':Order.STATUS_CANCELLED}, instance=obj)
                self.assertTrue(form.is_valid(), form.errors)
                admin.save_model(request, form.save(commit=False), form, True)
                admin.save_related(request, form, [], True)
                self.assertTrue(KookOutboxEvent.objects.filter(event_type='order.cancelled').exists())
                raise RuntimeError('rollback admin submission')
        obj.refresh_from_db()
        self.assertEqual(obj.status, Order.STATUS_WAITING)
        self.assertEqual(KookOutboxEvent.objects.count(), 1)


    def test_full_then_paid_still_updates_known_closed_stream(self):
        from . import kook_notifications as bridge, services
        from apps.payments.models import Payment
        from apps.payments.services import mark_payment_paid
        from apps.kook_integration.models import KookMessageProjection
        from apps.kook_integration.outbox import process_once
        from apps.kook_integration.client import SendResult
        from unittest.mock import Mock
        order = self.order(required_players=1)
        bridge.record_order_state(order.pk)
        KookMessageProjection.objects.filter(order_id=order.pk).update(remote_message_id='offline-old')
        services.grab_order(order.order_no, self.player)
        close = KookDelivery.objects.get(event__event_type='public_slots.closed')
        payment = Payment.objects.create(payment_no='OFFLINE-PUBLIC-PAY', order=order,
            channel='balance', scene='mini_program', amount=15, status='paying')
        mark_payment_paid(payment)
        self.assertTrue(bridge.revalidate_order(close.event.payload, close))
        client = Mock(bot_key='offline-test-bot')
        client.send.return_value = SendResult('sent', message_id='offline-old')
        with override_settings(KOOK_SEND_ENABLED=True, KOOK_CHANNEL_SEND_ENABLED=True,
                KOOK_TARGET_CONFIG_VERSION='v1', KOOK_VERIFIED_CONFIG_VERSION='v1',
                KOOK_VERIFIED_BOT_KEY='offline-test-bot', KOOK_VERIFIED_GUILD_ID='offline-guild',
                KOOK_TEST_ORDER_IDS=[order.pk], KOOK_ORDER_REVALIDATOR=bridge.revalidate_order):
            self.assertEqual(process_once(client=client)['outbound'], 1)
        self.assertEqual(client.send.call_args.kwargs['operation'], 'update')
        close.refresh_from_db()
        self.assertEqual(close.status, 'sent')
        self.assertEqual(close.remote_message_id, 'offline-old')

    def test_old_epoch_close_updates_only_old_message_after_reopen(self):
        from . import kook_notifications as bridge
        from apps.kook_integration.models import KookMessageProjection
        from apps.kook_integration.outbox import process_once
        from apps.kook_integration.client import SendResult
        from unittest.mock import Mock
        order = self.order(required_players=1)
        bridge.record_order_state(order.pk)
        old_open = KookDelivery.objects.get()
        KookMessageProjection.objects.filter(order_id=order.pk).update(remote_message_id='offline-old')
        order.order_players.create(player=self.player)
        bridge.record_order_state(order.pk)
        close = KookDelivery.objects.get(event__event_type='public_slots.closed')
        order.order_players.all().delete()
        bridge.record_order_state(order.pk)
        new_open = KookDelivery.objects.get(event__payload__public_epoch=1)
        KookMessageProjection.objects.filter(pk=new_open.stream_id).update(remote_message_id='offline-new')
        self.assertTrue(bridge.revalidate_order(close.event.payload, close))
        self.assertFalse(bridge.revalidate_order(old_open.event.payload, old_open))
        # Mutated stream identity, bot and no-message closures must fail closed.
        close.stream_id = new_open.stream_id
        self.assertFalse(bridge.revalidate_order(close.event.payload, close))
        close.refresh_from_db()
        for field, value in [('bot_key','foreign-bot'), ('remote_message_id','')]:
            stream = close.stream
            original = getattr(stream, field)
            KookMessageProjection.objects.filter(pk=stream.pk).update(**{field:value})
            self.assertFalse(bridge.revalidate_order(close.event.payload, close))
            KookMessageProjection.objects.filter(pk=stream.pk).update(**{field:original})
        client = Mock(bot_key='offline-test-bot')
        client.send.side_effect = lambda *a, **k: SendResult('sent', message_id=k['message_id'])
        with override_settings(KOOK_SEND_ENABLED=True, KOOK_CHANNEL_SEND_ENABLED=True,
                KOOK_TARGET_CONFIG_VERSION='v1', KOOK_VERIFIED_CONFIG_VERSION='v1',
                KOOK_VERIFIED_BOT_KEY='offline-test-bot', KOOK_VERIFIED_GUILD_ID='offline-guild',
                KOOK_TEST_ORDER_IDS=[order.pk], KOOK_ORDER_REVALIDATOR=bridge.revalidate_order):
            self.assertEqual(process_once(client=client)['outbound'], 2)
        close.refresh_from_db()
        new_open.refresh_from_db()
        self.assertEqual(close.remote_message_id, 'offline-old')
        self.assertEqual(new_open.remote_message_id, 'offline-new')
        self.assertEqual(close.status, 'sent')

    def test_replacement_terminal_snapshot_is_unambiguously_closed(self):
        from . import kook_notifications as bridge
        from .cancellation_models import OrderReplacementState
        from .replacements import request_cancel_remaining
        from apps.kook_integration.rendering import render
        order = self.order(paid=True, status=Order.STATUS_IN_PROGRESS)
        OrderReplacementState.objects.create(order=order, mode='public', status='open', resume_status=order.status)
        bridge.record_order_state(order.pk)
        request_cancel_remaining(order)
        event = KookOutboxEvent.objects.latest('occurred_at')
        self.assertEqual(event.event_type, 'replacement.resolved')
        self.assertEqual(event.payload['safe_snapshot']['status'], '补位已结束 · 不可接单')
        self.assertIn('不可接单', render(event))
        self.assertNotIn('进行中', render(event))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_IN_PROGRESS)

    def test_revoke_reopens_silently_even_if_first_delivery_is_coalesced(self):
        from . import kook_notifications as bridge
        from .cancellation_models import OrderReplacementState
        from .replacements import request_cancel_remaining, revoke_cancel_remaining
        from apps.kook_integration.outbox import process_once
        from apps.kook_integration.client import SendResult
        from unittest.mock import Mock
        order = self.order(paid=True, status=Order.STATUS_IN_PROGRESS)
        OrderReplacementState.objects.create(order=order, mode='public', status='open', resume_status=order.status)
        bridge.record_order_state(order.pk)
        request_cancel_remaining(order)
        revoke_cancel_remaining(order)
        event = KookOutboxEvent.objects.latest('occurred_at')
        self.assertEqual(event.event_type, 'public_slots.changed')
        self.assertTrue(bridge.public_open(order))
        order.boss_note = 'first delivery not yet sent'
        order.save(update_fields=['boss_note'])
        bridge.record_order_state(order.pk)
        self.assertEqual(KookOutboxEvent.objects.latest('occurred_at').event_type, 'public_slots.changed')
        client = Mock(bot_key='offline-test-bot')
        client.send.return_value = SendResult('sent', message_id='offline-silent')
        with override_settings(KOOK_SEND_ENABLED=True, KOOK_CHANNEL_SEND_ENABLED=True,
                KOOK_TARGET_CONFIG_VERSION='v1', KOOK_VERIFIED_CONFIG_VERSION='v1',
                KOOK_VERIFIED_BOT_KEY='offline-test-bot', KOOK_VERIFIED_GUILD_ID='offline-guild',
                KOOK_PRODUCTION_ENABLED=True, KOOK_MENTION_ALL_VERIFIED=True,
                KOOK_MENTION_FIRST_PUBLIC_ENABLED=True, KOOK_MENTION_REOPEN_ENABLED=True,
                KOOK_ORDER_REVALIDATOR=bridge.revalidate_order):
            self.assertEqual(process_once(client=client)['outbound'], 1)
        self.assertNotIn('(met)all(met)', client.send.call_args.args[2])

    def test_manual_targeted_payment_remains_explicit_business_blocker(self):
        # Characterization, NOT acceptance of the existing activation gap.
        from .boss_views import self_confirm_payment
        from . import kook_notifications as bridge
        from rest_framework.test import APIRequestFactory, force_authenticate
        admin = User.objects.create_superuser('offline-admin', password='offline')
        order = self.order(fulfillment_mode='targeted', target_player=self.player,
                           required_players=1, status=Order.STATUS_PENDING_PAYMENT)
        request = APIRequestFactory().post('/', {}, format='json')
        force_authenticate(request, user=admin)
        response = self_confirm_payment(request, order.order_no)
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertTrue(order.paid)
        self.assertFalse(order.designations.exists())
        bridge.record_order_state(order.pk)
        self.assertFalse(KookOutboxEvent.objects.exists())  # do not invent paid invitations


from django.test import TransactionTestCase
from django.db import connection, close_old_connections
from unittest import skipUnless


@skipUnless(connection.vendor == 'postgresql', 'requires isolated PostgreSQL row locks')
@override_settings(KOOK_ENABLED=True, KOOK_ORDER_EVENTS_ENABLED=True, KOOK_PRODUCTION_ENABLED=True, KOOK_VERIFIED_CHANNEL_ID='offline-channel')
class BridgeConcurrencyTests(TransactionTestCase):
    def test_close_commit_racing_reopen_preserves_old_terminal_and_single_epoch(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from . import kook_notifications as bridge
        from apps.kook_integration.models import KookMessageProjection
        package = Package.objects.create(name='offline-race', base_price=15, player_count=1)
        kind = PlayerType.objects.create(name='offline-race-type', priority=1)
        player = Player.objects.create(name='offline-race-player', status='approved', player_type=kind)
        order = Order.objects.create(order_no='OFFLINE-RACE', package=package, required_players=1)
        with transaction.atomic():
            bridge.record_order_state(order.pk)
        KookMessageProjection.objects.filter(order_id=order.pk).update(remote_message_id='offline-old')
        closed = Event()
        attempting_reopen = Event()

        def close_round():
            close_old_connections()
            try:
                with transaction.atomic():
                    locked = Order.objects.select_for_update().get(pk=order.pk)
                    OrderPlayer.objects.create(order=locked, player_id=player.pk)
                    bridge.record_order_state(order.pk)
                    closed.set()
                    if not attempting_reopen.wait(10):
                        raise AssertionError('reopen contender did not start')
            finally:
                close_old_connections()

        def reopen_round():
            close_old_connections()
            try:
                if not closed.wait(10):
                    raise AssertionError('close transaction did not reach boundary')
                with transaction.atomic():
                    attempting_reopen.set()
                    Order.objects.select_for_update().get(pk=order.pk)
                    OrderPlayer.objects.filter(order_id=order.pk).delete()
                    bridge.record_order_state(order.pk)
                    bridge.record_order_state(order.pk)  # duplicate does not advance epoch
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(close_round)
            b = pool.submit(reopen_round)
            a.result(timeout=20)
            b.result(timeout=20)
        self.assertEqual(KookOrderProjection.objects.get(order=order).public_epoch, 1)
        self.assertEqual(KookMessageProjection.objects.filter(order_id=order.pk).count(), 2)
        self.assertEqual(KookOutboxEvent.objects.filter(order_id=order.pk).count(), 3)
        close = KookDelivery.objects.get(event__event_type='public_slots.closed')
        self.assertTrue(bridge.revalidate_order(close.event.payload, close))
        initial = KookDelivery.objects.get(event__revision=1)
        self.assertFalse(bridge.revalidate_order(initial.event.payload, initial))
