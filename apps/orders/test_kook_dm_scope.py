"""Production DM scope regression, synthetic data and HTTP only."""
from django.test import TestCase, override_settings
from django.utils import timezone
from datetime import timedelta
from . import test_kook_no_link as canary
from . import kook_notifications as bridge
from apps.kook_integration.models import KookOrderProjection, KookOutboxEvent, KookDelivery

@override_settings(KOOK_ENABLED=True, KOOK_ORDER_EVENTS_ENABLED=True,
    KOOK_PRODUCTION_ENABLED=True, KOOK_TEST_ORDER_IDS=[],
    KOOK_ORDER_NOTIFICATION_SCOPE='designated_dm', KOOK_CHANNEL_SEND_ENABLED=False,
    KOOK_WECHAT_URL_LINK_ENABLED=False)
class ProductionDmScopeTests(TestCase):
    setUp = canary.NoLinkCanaryTests.setUp
    order = canary.NoLinkCanaryTests.order
    bind = canary.NoLinkCanaryTests.bind
    http_client = canary.NoLinkCanaryTests.http_client
    drain = canary.NoLinkCanaryTests.drain
    create_public = canary.NoLinkCanaryTests.create_public
    targeted_flow = canary.NoLinkCanaryTests.targeted_flow

    def send_flags(self, order=None):
        return override_settings(KOOK_SEND_ENABLED=True, KOOK_DM_SEND_ENABLED=True,
            KOOK_DM_ALLOWLIST=['offline-recipient'], KOOK_PLAYER_ALLOWLIST=[self.player.pk],
            KOOK_ORDER_DM_INVITATIONS_SINCE=(timezone.now()-timedelta(minutes=1)).isoformat(),
            KOOK_ORDER_REVALIDATOR=bridge.revalidate_order)

    def test_public_service_captures_nothing_when_channel_off(self):
        with self.send_flags():
            self.create_public()
        self.assertFalse(KookOrderProjection.objects.exists())
        self.assertFalse(KookOutboxEvent.objects.exists())
        self.assertFalse(KookDelivery.objects.exists())

    def test_unapproved_recipient_and_old_invitation_capture_nothing(self):
        from .designations import create_designations
        self.bind()
        order = self.order()
        with override_settings(KOOK_ORDER_EVENTS_ENABLED=False):
            create_designations(order, [self.player])
        for flags in ({'KOOK_DM_ALLOWLIST': []}, {'KOOK_PLAYER_ALLOWLIST': []},
                      {'KOOK_DM_SEND_ENABLED': False},
                      {'KOOK_ORDER_DM_INVITATIONS_SINCE': timezone.now().isoformat()},
                      {'KOOK_ORDER_DM_INVITATIONS_SINCE': ''}):
            with self.subTest(flags=flags), self.send_flags(), override_settings(**flags):
                self.assertEqual(bridge.record_order_state(order.pk), [])
                self.assertFalse(KookOrderProjection.objects.exists())
                self.assertFalse(KookOutboxEvent.objects.exists())

    def test_scope_and_cutoff_rechecked_before_send(self):
        from .designations import create_designations
        from apps.kook_integration.outbox import fresh
        self.bind()
        order = self.order()
        with canary.NoLinkCanaryTests.send_flags(self, order), override_settings(KOOK_ORDER_NOTIFICATION_SCOPE='all'):
            create_designations(order, [self.player])
            deliveries = list(KookDelivery.objects.select_related('event', 'stream'))
            self.assertEqual({d.scope for d in deliveries}, {'dm'})
            for delivery in deliveries:
                self.assertEqual(fresh(delivery), (True, ''))
                with override_settings(KOOK_ORDER_NOTIFICATION_SCOPE='designated_dm',
                        KOOK_ORDER_DM_INVITATIONS_SINCE=timezone.now().isoformat()):
                    self.assertFalse(fresh(delivery)[0])

    def test_only_approved_invitation_is_snapshotted(self):
        from .designations import create_designations
        self.bind()
        other = canary.NoLinkCanaryTests.another_player(self, 'unapproved')
        order = self.order()
        with self.send_flags():
            create_designations(order, [self.player, other])
        self.assertEqual(KookOutboxEvent.objects.count(), 1)
        self.assertEqual(KookDelivery.objects.get().binding.player_id, self.player.pk)
        state = KookOrderProjection.objects.get().latest_safe_snapshot
        self.assertEqual([v['player_id'] for v in state['invitations'].values()], [self.player.pk])
        self.assertFalse(state['public_open'])

    def test_targeted_accept(self):
        self.targeted_flow('accept')

    def test_targeted_decline(self):
        self.targeted_flow('decline')

    def test_targeted_expiry(self):
        self.targeted_flow('expire')
