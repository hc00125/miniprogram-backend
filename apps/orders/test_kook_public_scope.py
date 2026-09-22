"""Fixed public rollout boundary, isolated ORM/HTTP only."""
from django.test import TestCase, override_settings
from django.utils import timezone
from apps.kook_integration.models import KookDelivery, KookOrderProjection, KookOutboxEvent
from apps.kook_integration.outbox import fresh, process_once
from . import kook_notifications as bridge
from . import test_kook_all_scope as fixtures

@override_settings(KOOK_ENABLED=True, KOOK_ORDER_EVENTS_ENABLED=True,
    KOOK_PRODUCTION_ENABLED=True, KOOK_ORDER_NOTIFICATION_SCOPE='all',
    KOOK_VERIFIED_CHANNEL_ID='offline-channel', KOOK_WECHAT_URL_LINK_ENABLED=False)
class PublicScopeTests(TestCase):
    def test_existing_old_card_can_close_after_intermediate_update_but_never_reopen(self):
        from .models import Order
        order = self.order()
        client, http = self.http_client()
        with self.flags(order), override_settings(KOOK_ORDER_PUBLIC_CREATED_SINCE=order.created_at.isoformat()):
            bridge.record_order_state(order.pk)
            self.assertEqual(process_once(client=client)['outbound'], 1)
            original = http.post.call_count
            with override_settings(KOOK_ORDER_PUBLIC_CREATED_SINCE=timezone.now().isoformat()):
                order.boss_note = 'older card business update'
                order.save(update_fields=['boss_note'])
                bridge.record_order_state(order.pk)
                process_once(client=client)
                self.assertEqual(http.post.call_count, original)
                order.status = Order.STATUS_CANCELLED
                order.save(update_fields=['status'])
                bridge.record_order_state(order.pk)
                self.assertEqual(process_once(client=client)['outbound'], 1)
                self.assertTrue(http.post.call_args.args[0].endswith('message/update'))
                order.status = Order.STATUS_WAITING
                order.save(update_fields=['status'])
                self.assertEqual(bridge.record_order_state(order.pk), [])
                self.assertEqual(process_once(client=client)['outbound'], 0)
                self.assertEqual(http.post.call_count, original + 1)

    def test_first_public_mention_requires_independent_flag(self):
        order = self.order()
        client, http = self.http_client()
        with self.flags(order), override_settings(KOOK_ORDER_PUBLIC_CREATED_SINCE=order.created_at.isoformat(),
                KOOK_MENTION_ALL_VERIFIED=True, KOOK_MENTION_FIRST_PUBLIC_ENABLED=False):
            bridge.record_order_state(order.pk)
            self.assertEqual(process_once(client=client)['outbound'], 1)
            self.assertNotIn('(met)all(met)', http.post.call_args.kwargs['json']['content'])

    def test_first_mentions_once_replacement_and_retries_never_mention(self):
        from apps.players.permission_views import grab_order_service
        from .player_cancellations import cancel_player_order
        from .models import Order
        second = fixtures.AllScopeGuardTests.another_player(self, 'public-second')
        order = self.order(paid=True)
        order.boss_note = '(met)all(met) @全体 (rol)123(rol)'
        order.save(update_fields=['boss_note'])
        client, http = self.http_client()
        with self.flags(order), override_settings(KOOK_ORDER_PUBLIC_CREATED_SINCE=order.created_at.isoformat(),
                KOOK_MENTION_ALL_VERIFIED=True, KOOK_MENTION_FIRST_PUBLIC_ENABLED=True,
                KOOK_MENTION_REOPEN_ENABLED=False):
            bridge.record_order_state(order.pk)
            self.assertEqual(process_once(client=client)['outbound'], 1)
            self.assertEqual(http.post.call_args.kwargs['json']['content'].count('(met)all(met)'), 1)
            self.assertEqual(process_once(client=client)['outbound'], 0)
            grab_order_service(order.order_no, self.player)
            self.assertEqual(process_once(client=client)['outbound'], 1)
            self.assertNotIn('(met)all(met)', http.post.call_args.kwargs['json']['content'])
            grab_order_service(order.order_no, second)
            self.assertEqual(process_once(client=client)['outbound'], 1)
            cancel_player_order(order.order_no, self.player, 'isolated replacement')
            self.assertEqual(process_once(client=client)['outbound'], 1)
            self.assertEqual(KookOrderProjection.objects.get(order=order).public_epoch, 1)
            self.assertTrue(http.post.call_args.args[0].endswith('message/create'))
            self.assertNotIn('(met)all(met)', http.post.call_args.kwargs['json']['content'])
            self.assertEqual(process_once(client=client)['outbound'], 0)

    def test_http_retry_does_not_repeat_first_mention(self):
        from unittest.mock import Mock
        from apps.kook_integration.models import KookRateLimit
        order = self.order()
        client, http = self.http_client()
        original_post = http.post.side_effect
        http.post.side_effect = lambda *a, **kw: Mock(status_code=429, headers={'Retry-After': '1'}, json=lambda: {'code': 429})
        with self.flags(order), override_settings(KOOK_ORDER_PUBLIC_CREATED_SINCE=order.created_at.isoformat(),
                KOOK_MENTION_ALL_VERIFIED=True, KOOK_MENTION_FIRST_PUBLIC_ENABLED=True):
            bridge.record_order_state(order.pk)
            self.assertEqual(process_once(client=client)['outbound'], 1)
            self.assertIn('(met)all(met)', http.post.call_args.kwargs['json']['content'])
            KookDelivery.objects.update(next_attempt_at=timezone.now())
            KookRateLimit.objects.update(blocked_until=timezone.now())
            http.post.side_effect = original_post
            self.assertEqual(process_once(client=client)['outbound'], 1)
            self.assertNotIn('(met)all(met)', http.post.call_args.kwargs['json']['content'])

    setUp = fixtures.AllScopeGuardTests.setUp
    order = fixtures.AllScopeGuardTests.order
    flags = fixtures.AllScopeGuardTests.flags
    http_client = fixtures.AllScopeGuardTests.http_client

    def test_historical_public_order_never_captured(self):
        order = self.order()
        with self.flags(order), override_settings(KOOK_ORDER_PUBLIC_CREATED_SINCE=timezone.now().isoformat()):
            self.assertEqual(bridge.record_order_state(order.pk), [])
            self.assertFalse(KookOrderProjection.objects.exists())
            self.assertFalse(KookOutboxEvent.objects.exists())
            self.assertFalse(KookDelivery.objects.exists())

    def test_queued_old_public_create_is_rejected_at_send(self):
        order = self.order()
        with self.flags(order), override_settings(KOOK_ORDER_PUBLIC_CREATED_SINCE=order.created_at.isoformat()):
            bridge.record_order_state(order.pk)
            delivery = KookDelivery.objects.select_related('event', 'stream').get(scope='channel')
            self.assertTrue(fresh(delivery)[0])
            with override_settings(KOOK_ORDER_PUBLIC_CREATED_SINCE=timezone.now().isoformat()):
                self.assertFalse(fresh(delivery)[0])
                client, http = self.http_client()
                self.assertEqual(process_once(client=client)['outbound'], 0)
                http.post.assert_not_called()
