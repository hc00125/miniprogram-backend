"""Explicit designated-DM opening: real ORM/worker, mocked HTTP only."""
from django.test import TestCase, override_settings
from django.utils import timezone
from datetime import timedelta
from . import test_kook_no_link as fixtures
from . import kook_notifications as bridge
from .designations import create_designations
from apps.kook_integration.models import KookDelivery
from apps.kook_integration.outbox import fresh, process_once

@override_settings(KOOK_ENABLED=True, KOOK_ORDER_EVENTS_ENABLED=True,
    KOOK_PRODUCTION_ENABLED=True, KOOK_ORDER_NOTIFICATION_SCOPE='designated_dm',
    KOOK_CHANNEL_SEND_ENABLED=False, KOOK_WECHAT_URL_LINK_ENABLED=False,
    KOOK_SEND_ENABLED=True, KOOK_DM_SEND_ENABLED=True,
    KOOK_DM_ALLOWLIST=[], KOOK_PLAYER_ALLOWLIST=[],
    KOOK_ORDER_DM_INVITATIONS_SINCE='2020-01-01T00:00:00+00:00',
    KOOK_DESIGNATED_DM_OPEN_ENABLED=True,
    KOOK_DESIGNATED_DM_OPEN_SINCE='2020-01-01T00:00:00+00:00',
    KOOK_ORDER_REVALIDATOR=bridge.revalidate_order)
class FormalOpenTests(TestCase):
    setUp = fixtures.NoLinkCanaryTests.setUp
    order = fixtures.NoLinkCanaryTests.order
    bind = fixtures.NoLinkCanaryTests.bind
    http_client = fixtures.NoLinkCanaryTests.http_client

    def test_non_allowlisted_designated_recipient_captures_and_sends_once(self):
        self.bind()
        order = self.order()
        create_designations(order, [self.player])
        self.assertEqual(KookDelivery.objects.count(), 1)
        delivery = KookDelivery.objects.select_related('event','stream').get()
        self.assertEqual(fresh(delivery), (True, ''))
        client, http = self.http_client()
        self.assertEqual(process_once(client=client)['outbound'], 1)
        self.assertEqual(http.post.call_args.kwargs['json']['target_id'], 'offline-recipient')
        self.assertNotIn('(met)all(met)', http.post.call_args.kwargs['json']['content'])
        self.assertEqual(process_once(client=client)['outbound'], 0)
        self.assertEqual(http.post.call_count, 1)

    def test_opening_does_not_capture_old_nonallowlisted_invitation(self):
        self.bind()
        order = self.order()
        with override_settings(KOOK_ORDER_EVENTS_ENABLED=False):
            create_designations(order, [self.player])
        with override_settings(KOOK_DESIGNATED_DM_OPEN_SINCE=timezone.now().isoformat()):
            self.assertEqual(bridge.record_order_state(order.pk), [])
            self.assertFalse(KookDelivery.objects.exists())

    def test_opening_cutoff_rechecked_on_existing_queue(self):
        self.bind()
        order = self.order()
        create_designations(order, [self.player])
        delivery = KookDelivery.objects.select_related('event','stream').get()
        self.assertEqual(fresh(delivery), (True, ''))
        for cutoff in ('', None, 'invalid', '2026-99-99T00:00:00+00:00', '2020-01-01T00:00:00', timezone.now().isoformat()):
            with self.subTest(cutoff=cutoff), override_settings(KOOK_DESIGNATED_DM_OPEN_SINCE=cutoff):
                self.assertFalse(fresh(delivery)[0])

    def test_switch_off_and_none_or_empty_lists_fail_closed(self):
        from apps.kook_integration.outbox import send_allowed
        binding = self.bind()
        order = self.order()
        with override_settings(KOOK_ORDER_EVENTS_ENABLED=False):
            create_designations(order, [self.player])
        for enabled in (False, None, 'true', 1):
            for users, players in (([], []), (None, None), (['offline-recipient'], []), ([], [self.player.pk])):
                with self.subTest(enabled=enabled, users=users, players=players), override_settings(
                        KOOK_DESIGNATED_DM_OPEN_ENABLED=enabled, KOOK_DM_ALLOWLIST=users, KOOK_PLAYER_ALLOWLIST=players):
                    self.assertFalse(send_allowed(binding, designated=True))
                    self.assertEqual(bridge.record_order_state(order.pk), [])
        with override_settings(KOOK_DESIGNATED_DM_OPEN_ENABLED=False,
                KOOK_DM_ALLOWLIST=['offline-recipient'], KOOK_PLAYER_ALLOWLIST=[self.player.pk],
                KOOK_DESIGNATED_DM_OPEN_SINCE=''):
            self.assertTrue(send_allowed(binding, designated=True))
            bridge.record_order_state(order.pk)
            self.assertEqual(fresh(KookDelivery.objects.select_related('event','stream').get()), (True, ''))

    def test_open_switch_does_not_open_test_dm_or_public_channel(self):
        import uuid
        from apps.kook_integration.outbox import send_allowed, enqueue_test
        from apps.kook_integration.secrets import KookError
        binding = self.bind()
        self.assertFalse(send_allowed(binding))
        self.assertFalse(send_allowed(scope='channel'))
        with self.assertRaises(KookError):
            enqueue_test(self.player, uuid.uuid4(), 'offline-ip')
        self.assertFalse(KookDelivery.objects.exists())

    def test_all_business_gates_rechecked_in_both_scopes(self):
        from django.db import transaction
        from uuid import uuid4
        binding = self.bind()
        order = self.order()
        create_designations(order, [self.player])
        row = order.designations.get()
        delivery = KookDelivery.objects.select_related('event','stream').get()
        profile = self.player.user.client_profile
        mutations = [(binding, 'active', False), (binding, 'notifications_enabled', False),
            (binding, 'version', uuid4()), (binding, 'bot_key', 'other-bot'),
            (binding, 'kook_user_id', 'different-target'),
            (self.player, 'status', 'pending'), (self.player, 'can_accept_orders', False),
            (self.player, 'can_be_designated', False), (self.player.user, 'is_active', False),
            (profile, 'account_status', 'disabled'), (profile, 'openid', ''),
            (row, 'status', 'cancelled'), (row, 'expires_at', timezone.now()-timedelta(seconds=1)),
            (order, 'status', 'cancelled'), (delivery.event, 'expires_at', timezone.now()-timedelta(seconds=1))]
        for scope in ('all', 'designated_dm'):
            with override_settings(KOOK_ORDER_NOTIFICATION_SCOPE=scope):
                self.assertEqual(fresh(delivery), (True, ''))
                for obj, field, value in mutations:
                    with self.subTest(scope=scope, model=obj._meta.label, field=field), transaction.atomic():
                        type(obj).objects.filter(pk=obj.pk).update(**{field:value})
                        current = KookDelivery.objects.select_related('event','stream').get(pk=delivery.pk)
                        self.assertFalse(fresh(current)[0])
                        client, http = self.http_client()
                        self.assertEqual(process_once(client=client)['outbound'], 0)
                        http.post.assert_not_called()
                        transaction.set_rollback(True)
                for flag in ('KOOK_ENABLED','KOOK_SEND_ENABLED','KOOK_DM_SEND_ENABLED',
                             'KOOK_ORDER_EVENTS_ENABLED','KOOK_DESIGNATED_DM_OPEN_ENABLED'):
                    with self.subTest(scope=scope, flag=flag), override_settings(**{flag:False}):
                        self.assertFalse(fresh(delivery)[0])

    def test_capture_eligibility_and_non_designated_target(self):
        from django.db import transaction
        from apps.kook_integration.models import KookBinding
        binding = self.bind()
        other = fixtures.NoLinkCanaryTests.another_player(self, 'not-invited')
        other_binding = KookBinding.objects.create(bot_key='offline-test-bot', player=other,
            kook_user_id='offline-not-invited', notifications_enabled=True, verified_at=timezone.now())
        order = self.order()
        with override_settings(KOOK_ORDER_EVENTS_ENABLED=False):
            create_designations(order, [self.player])
        row = order.designations.get()
        for obj, field, value in ((binding,'active',False), (binding,'notifications_enabled',False),
                (binding,'bot_key','wrong-bot'), (self.player,'status','pending'),
                (self.player,'can_accept_orders',False), (self.player,'can_be_designated',False),
                (self.player.user,'is_active',False), (self.player.user.client_profile,'account_status','disabled')):
            with self.subTest(field=field), transaction.atomic():
                type(obj).objects.filter(pk=obj.pk).update(**{field:value})
                self.assertIsNone(bridge.scoped_dm_binding(row))
                self.assertEqual(bridge.record_order_state(order.pk), [])
                transaction.set_rollback(True)
        bridge.record_order_state(order.pk)
        self.assertEqual(list(KookDelivery.objects.values_list('binding_id',flat=True)), [binding.pk])
        delivery = KookDelivery.objects.select_related('event','stream').get()
        delivery.binding = other_binding
        delivery.binding_version = other_binding.version
        delivery.target_id = other_binding.kook_user_id
        self.assertFalse(fresh(delivery)[0])

    def test_opening_cutoff_inclusive_and_escort_qualification_required(self):
        from apps.players.escort_models import OrderEscortRequirementSnapshot
        from apps.players.models import PlayerEscortQualification
        self.bind()
        order = self.order(required_players=1)
        with override_settings(KOOK_ORDER_EVENTS_ENABLED=False):
            create_designations(order, [self.player])
        row = order.designations.get()
        OrderEscortRequirementSnapshot.objects.update_or_create(order=order,
            defaults={'requires_escort_qualification': True})
        with override_settings(KOOK_DESIGNATED_DM_OPEN_SINCE=row.invited_at.isoformat()):
            self.assertEqual(bridge.record_order_state(order.pk), [])
            qualification = PlayerEscortQualification.objects.create(player=self.player, status='approved')
            bridge.record_order_state(order.pk)
            delivery = KookDelivery.objects.select_related('event','stream').get()
            self.assertEqual(fresh(delivery), (True, ''))
            qualification.status = 'suspended'
            qualification.save(update_fields=['status'])
            self.assertFalse(fresh(delivery)[0])
