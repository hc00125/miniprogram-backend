"""Cross-scope DM authorization; synthetic ORM and mocked HTTP only."""
from uuid import uuid4
from django.test import TestCase, override_settings
from django.utils import timezone
from apps.kook_integration.models import KookBinding, KookDelivery, KookOrderProjection, KookOutboxEvent
from apps.kook_integration.outbox import fresh, process_once
from . import kook_notifications as bridge
from . import test_kook_no_link as canary
from .designations import create_designations


@override_settings(KOOK_ENABLED=True, KOOK_ORDER_EVENTS_ENABLED=True,
    KOOK_PRODUCTION_ENABLED=True, KOOK_ORDER_NOTIFICATION_SCOPE='all',
    KOOK_VERIFIED_CHANNEL_ID='offline-channel', KOOK_WECHAT_URL_LINK_ENABLED=False,
    KOOK_MENTION_ALL_VERIFIED=False)
class AllScopeGuardTests(TestCase):
    setUp = canary.NoLinkCanaryTests.setUp
    order = canary.NoLinkCanaryTests.order
    bind = canary.NoLinkCanaryTests.bind
    another_player = canary.NoLinkCanaryTests.another_player
    http_client = canary.NoLinkCanaryTests.http_client

    def flags(self, order):
        return override_settings(KOOK_SEND_ENABLED=True, KOOK_DM_SEND_ENABLED=True,
            KOOK_CHANNEL_SEND_ENABLED=True, KOOK_TARGET_CONFIG_VERSION='guard',
            KOOK_VERIFIED_CONFIG_VERSION='guard', KOOK_VERIFIED_BOT_KEY='offline-test-bot',
            KOOK_VERIFIED_GUILD_ID='offline-guild',
            KOOK_DM_ALLOWLIST=['offline-recipient'], KOOK_PLAYER_ALLOWLIST=[self.player.pk],
            KOOK_ORDER_DM_INVITATIONS_SINCE='2020-01-01T00:00:00+00:00',
            KOOK_ORDER_REVALIDATOR=bridge.revalidate_order)

    def test_all_only_self_new_invitation_persists_and_sends(self):
        self.bind()
        other = self.another_player('not-authorized')
        KookBinding.objects.create(bot_key='offline-test-bot', player=other,
            kook_user_id='not-allowlisted', notifications_enabled=True, verified_at=timezone.now())
        order = self.order(required_players=3)
        client, http = self.http_client()
        with self.flags(order):
            create_designations(order, [self.player, other])
            state = KookOrderProjection.objects.get(order=order).latest_safe_snapshot
            self.assertEqual([v['player_id'] for v in state['invitations'].values()], [self.player.pk])
            self.assertEqual(KookOutboxEvent.objects.filter(designation_id__isnull=False).count(), 1)
            self.assertEqual(KookDelivery.objects.get(scope='dm').binding.player_id, self.player.pk)
            public = KookOutboxEvent.objects.get(payload__audience='public_channel').payload['safe_snapshot']
            self.assertEqual(set(public), {'published_at', 'participant_count', 'player_type_label',
                'boss_note', 'status', 'slot_summary'})
            self.assertNotIn('NEVER_PUBLIC', str(public))
            self.assertEqual(public['boss_note'], '联系我 13800138000')
            self.assertEqual(process_once(client=client)['outbound'], 2)
            self.assertEqual(http.post.call_count, 2)
            self.assertEqual({c.kwargs['json']['target_id'] for c in http.post.call_args_list},
                {'offline-channel', 'offline-recipient'})
            self.assertTrue(all('(met)all(met)' not in c.kwargs['json']['content'] for c in http.post.call_args_list))
            self.assertEqual(process_once(client=client)['outbound'], 0)

    def test_all_invalid_cutoff_fails_closed_but_public_still_sends(self):
        self.bind()
        order = self.order()
        with override_settings(KOOK_ORDER_EVENTS_ENABLED=False):
            create_designations(order, [self.player])
        for cutoff in ('', 'invalid', '2026-99-99T00:00:00+00:00', '2020-01-01T00:00:00', None):
            with self.subTest(cutoff=cutoff), self.flags(order), override_settings(KOOK_ORDER_DM_INVITATIONS_SINCE=cutoff):
                bridge.record_order_state(order.pk)
                self.assertEqual(KookOrderProjection.objects.get(order=order).latest_safe_snapshot['invitations'], {})
                self.assertFalse(KookDelivery.objects.filter(scope='dm').exists())
                delivery = KookDelivery.objects.select_related('event', 'stream').get(scope='channel')
                self.assertEqual(fresh(delivery), (True, ''))
        with self.flags(order), override_settings(KOOK_ORDER_DM_INVITATIONS_SINCE=''):
            client, http = self.http_client()
            self.assertEqual(process_once(client=client)['outbound'], 1)
            self.assertEqual(http.post.call_args.kwargs['json']['target_id'], 'offline-channel')

    def test_capture_binding_consent_bot_allowlists_and_eligibility_shared(self):
        binding = self.bind()
        order = self.order(required_players=1)
        with override_settings(KOOK_ORDER_EVENTS_ENABLED=False):
            create_designations(order, [self.player])
        mutations = [(binding, 'active', False), (binding, 'notifications_enabled', False),
            (binding, 'bot_key', 'another-bot'), (self.player, 'can_accept_orders', False),
            (self.player, 'status', 'pending'), (self.player.user, 'is_active', False),
            (self.player.user.client_profile, 'account_status', 'disabled'),
            (self.player.user.client_profile, 'openid', '')]
        for scope in ('all', 'designated_dm'):
            for obj, field, value in mutations:
                original = getattr(obj, field)
                try:
                    setattr(obj, field, value)
                    obj.save(update_fields=[field])
                    with self.subTest(scope=scope, field=field), self.flags(order), override_settings(KOOK_ORDER_NOTIFICATION_SCOPE=scope):
                        bridge.record_order_state(order.pk)
                        self.assertFalse(KookOutboxEvent.objects.exists())
                        self.assertFalse(KookDelivery.objects.exists())
                finally:
                    setattr(obj, field, original)
                    obj.save(update_fields=[field])
            for flags in ({'KOOK_DM_ALLOWLIST': []}, {'KOOK_PLAYER_ALLOWLIST': []},
                          {'KOOK_DM_SEND_ENABLED': False}, {'KOOK_SEND_ENABLED': False}):
                with self.subTest(scope=scope, flags=flags), self.flags(order), override_settings(KOOK_ORDER_NOTIFICATION_SCOPE=scope, **flags):
                    bridge.record_order_state(order.pk)
                    self.assertFalse(KookOutboxEvent.objects.exists())
                    self.assertFalse(KookDelivery.objects.exists())

    def test_queued_revocations_rechecked_in_both_scopes(self):
        binding = self.bind()
        order = self.order(required_players=1)
        with self.flags(order):
            create_designations(order, [self.player])
            dm = KookDelivery.objects.select_related('event', 'stream').get(scope='dm')
            for scope in ('all', 'designated_dm'):
                with override_settings(KOOK_ORDER_NOTIFICATION_SCOPE=scope):
                    self.assertEqual(fresh(dm), (True, ''))
                    for flags in ({'KOOK_DM_ALLOWLIST': []}, {'KOOK_PLAYER_ALLOWLIST': []},
                                  {'KOOK_ORDER_DM_INVITATIONS_SINCE': ''},
                                  {'KOOK_ORDER_NOTIFICATION_SCOPE': 'disabled'}):
                        with self.subTest(scope=scope, flags=flags), override_settings(**flags):
                            self.assertFalse(fresh(dm)[0])
                    for obj, field, value in ((binding, 'active', False), (binding, 'notifications_enabled', False),
                            (binding, 'version', uuid4()), (binding, 'bot_key', 'wrong-bot'),
                            (self.player, 'can_be_designated', False), (self.player, 'can_accept_orders', False)):
                        original = getattr(obj, field)
                        try:
                            setattr(obj, field, value)
                            obj.save(update_fields=[field])
                            with self.subTest(scope=scope, field=field):
                                self.assertFalse(fresh(dm)[0])
                        finally:
                            setattr(obj, field, original)
                            obj.save(update_fields=[field])
            with override_settings(KOOK_PLAYER_ALLOWLIST=[]):
                client, http = self.http_client()
                self.assertEqual(process_once(client=client)['outbound'], 0)
                http.post.assert_not_called()

    def test_scope_narrowing_blocks_queued_public_preserves_self_dm(self):
        self.bind()
        order = self.order()
        with self.flags(order):
            create_designations(order, [self.player])
            channel = KookDelivery.objects.select_related('event', 'stream').get(scope='channel')
            dm = KookDelivery.objects.select_related('event', 'stream').get(scope='dm')
            with override_settings(KOOK_ORDER_NOTIFICATION_SCOPE='designated_dm'):
                self.assertFalse(fresh(channel)[0])
                self.assertEqual(fresh(dm), (True, ''))
                public_count = KookOutboxEvent.objects.filter(payload__audience='public_channel').count()
                order.boss_note = 'scope narrowed'
                order.save(update_fields=['boss_note'])
                bridge.record_order_state(order.pk)
                self.assertEqual(KookOutboxEvent.objects.filter(payload__audience='public_channel').count(), public_count)
                client, http = self.http_client()
                self.assertEqual(process_once(client=client)['outbound'], 1)
                self.assertEqual(http.post.call_args.kwargs['json']['target_id'], 'offline-recipient')

    def test_cutoff_is_inclusive_and_scope_switch_does_not_replay(self):
        self.bind()
        order = self.order(required_players=1)
        with override_settings(KOOK_ORDER_EVENTS_ENABLED=False):
            create_designations(order, [self.player])
        row = order.designations.get()
        with self.flags(order), override_settings(KOOK_ORDER_DM_INVITATIONS_SINCE=row.invited_at.isoformat()):
            with override_settings(KOOK_ORDER_NOTIFICATION_SCOPE='designated_dm'):
                bridge.record_order_state(order.pk)
            dm = KookDelivery.objects.select_related('event', 'stream').get(scope='dm')
            self.assertEqual(fresh(dm), (True, ''))
            self.assertEqual(bridge.record_order_state(order.pk), [])
            self.assertEqual(KookOutboxEvent.objects.count(), 1)

    def test_escort_qualification_shared_by_capture_and_send(self):
        from apps.players.escort_models import OrderEscortRequirementSnapshot
        from apps.players.models import PlayerEscortQualification
        self.bind()
        order = self.order(required_players=1)
        with override_settings(KOOK_ORDER_EVENTS_ENABLED=False):
            create_designations(order, [self.player])
        OrderEscortRequirementSnapshot.objects.update_or_create(order=order,
            defaults={'requires_escort_qualification': True})
        with self.flags(order):
            bridge.record_order_state(order.pk)
            self.assertFalse(KookOutboxEvent.objects.exists())
            qualification = PlayerEscortQualification.objects.create(player=self.player, status='approved')
            bridge.record_order_state(order.pk)
            dm = KookDelivery.objects.select_related('event', 'stream').get(scope='dm')
            self.assertEqual(fresh(dm), (True, ''))
            qualification.status = 'suspended'
            qualification.save(update_fields=['status'])
            self.assertFalse(fresh(dm)[0])

    def test_ineligible_designation_not_captured_in_either_scope(self):
        self.bind()
        order = self.order(required_players=1)
        with override_settings(KOOK_ORDER_EVENTS_ENABLED=False):
            create_designations(order, [self.player])
        self.player.can_be_designated = False
        self.player.save(update_fields=['can_be_designated'])
        for scope in ('all', 'designated_dm'):
            with self.subTest(scope=scope), self.flags(order), override_settings(KOOK_ORDER_NOTIFICATION_SCOPE=scope):
                bridge.record_order_state(order.pk)
                self.assertFalse(KookOutboxEvent.objects.filter(designation_id__isnull=False).exists())
                self.assertFalse(KookDelivery.objects.filter(scope='dm').exists())

    def test_all_queued_dm_rechecks_cutoff_before_transport(self):
        self.bind()
        order = self.order(required_players=1)
        client, http = self.http_client()
        with self.flags(order):
            create_designations(order, [self.player])
            dm = KookDelivery.objects.select_related('event', 'stream').get(scope='dm')
            self.assertEqual(fresh(dm), (True, ''))
            with override_settings(KOOK_ORDER_DM_INVITATIONS_SINCE=timezone.now().isoformat()):
                self.assertFalse(fresh(dm)[0])
                self.assertEqual(process_once(client=client)['outbound'], 0)
                http.post.assert_not_called()

    def test_all_historical_self_invitation_not_persisted_or_queued(self):
        self.bind()
        order = self.order()
        with override_settings(KOOK_ORDER_EVENTS_ENABLED=False):
            create_designations(order, [self.player])
        with self.flags(order), override_settings(KOOK_ORDER_DM_INVITATIONS_SINCE=timezone.now().isoformat()):
            bridge.record_order_state(order.pk)
        self.assertEqual(KookOrderProjection.objects.get(order=order).latest_safe_snapshot['invitations'], {})
        self.assertFalse(KookOutboxEvent.objects.filter(designation_id__isnull=False).exists())
        self.assertFalse(KookDelivery.objects.filter(scope='dm').exists())
        self.assertTrue(KookOutboxEvent.objects.filter(payload__audience='public_channel').exists())
