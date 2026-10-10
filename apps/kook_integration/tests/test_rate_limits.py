import uuid
from datetime import timedelta
from unittest.mock import Mock, patch
from django.test import TestCase, override_settings
from django.utils import timezone
from apps.kook_integration import outbox
from apps.kook_integration.client import KookClient, SendResult
from apps.kook_integration.models import KookOutboxEvent


@override_settings(KOOK_ENABLED=True, KOOK_SEND_ENABLED=True, KOOK_CHANNEL_SEND_ENABLED=True,
                   KOOK_TARGET_CONFIG_VERSION='test', KOOK_VERIFIED_CONFIG_VERSION='test',
                   KOOK_VERIFIED_BOT_KEY='offline-test-bot', KOOK_VERIFIED_CHANNEL_ID='test-channel',
                   KOOK_VERIFIED_GUILD_ID='test-guild', KOOK_PRODUCTION_ENABLED=True, KOOK_ORDER_EVENTS_ENABLED=True,
                   KOOK_ORDER_REVALIDATOR=lambda envelope, delivery: True)
class SharedRateLimitTests(TestCase):
    def queue(self):
        event = KookOutboxEvent.objects.create(event_type='public_slots.opened',
            source_key=str(uuid.uuid4()), order_id=KookOutboxEvent.objects.count()+1,
            expires_at=timezone.now()+timedelta(minutes=10),
            payload={'audience':'public_channel', 'safe_snapshot':{}})
        return outbox.schedule_delivery(event, 'channel')

    def test_old_bot_parked_with_new_bot_hold_and_remote_id(self):
        for remote in ('', 'old-message'):
            delivery = self.queue()
            delivery.stream.remote_message_id = remote
            delivery.stream.save()
            client = Mock(bot_key='replacement-bot')
            client.send.return_value = SendResult('sent', message_id='wrong-bot')
            with override_settings(KOOK_BOT_KEY='replacement-bot', KOOK_VERIFIED_BOT_KEY='replacement-bot'):
                outbox.remember_rate_limit('replacement-bot', SendResult('retry', retry_after=90, global_limit=True))
                self.assertEqual(outbox.process_once(client=client)['outbound'], 0)
                self.assertIsNone(outbox.claim_delivery(delivery.pk))
            client.send.assert_not_called()
            delivery.refresh_from_db()
            self.assertEqual(delivery.status, 'queued')
            self.assertEqual(delivery.last_error_code, 'BOT_IDENTITY_MISMATCH')
            self.assertEqual(delivery.attempt_count, 0)

    def test_client_identity_and_post_link_switch_are_fenced(self):
        delivery = self.queue()
        client = Mock(bot_key='other-bot')
        client.send.return_value = SendResult('sent', message_id='wrong-client')
        self.assertEqual(outbox.process_once(client=client)['outbound'], 0)
        client.send.assert_not_called()
        client.bot_key = 'offline-test-bot'
        def switch(_):
            client.bot_key = 'other-bot'
            return ''
        with patch('apps.kook_integration.navigation.delivery_url_link', side_effect=switch):
            self.assertEqual(outbox.process_once(client=client)['outbound'], 0)
        client.send.assert_not_called()
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'queued')
        self.assertEqual(delivery.last_error_code, 'BOT_IDENTITY_MISMATCH')

    def test_settings_switch_after_claim_parks_without_http(self):
        delivery = self.queue()
        client = Mock(bot_key='offline-test-bot')
        original = outbox.claim_delivery
        from django.conf import settings
        with override_settings(KOOK_BOT_KEY='offline-test-bot'):
            def claim(pk):
                result = original(pk)
                settings.KOOK_BOT_KEY = 'replacement-bot'
                return result
            with patch.object(outbox, 'claim_delivery', side_effect=claim):
                self.assertEqual(outbox.process_once(client=client)['outbound'], 0)
        client.send.assert_not_called()
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'queued')
        self.assertEqual(delivery.last_error_code, 'BOT_IDENTITY_MISMATCH')
        self.assertEqual(delivery.next_attempt_at.year, 9999)

    def test_persisted_stream_identity_rechecked_after_link(self):
        delivery = self.queue()
        client = Mock(bot_key='offline-test-bot')
        client.send.return_value = SendResult('sent', message_id='must-not-send')
        def changed(_):
            outbox.KookMessageProjection.objects.filter(pk=delivery.stream_id).update(bot_key='foreign-bot')
            return ''
        with patch('apps.kook_integration.navigation.delivery_url_link', side_effect=changed):
            self.assertEqual(outbox.process_once(client=client)['outbound'], 0)
        client.send.assert_not_called()
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, 'queued')
        self.assertEqual(delivery.last_error_code, 'BOT_IDENTITY_MISMATCH')

    def test_direct_claim_rejects_foreign_stream_before_gate(self):
        delivery = self.queue()
        with override_settings(KOOK_BOT_KEY='replacement-bot'):
            self.assertIsNone(outbox.claim_delivery(delivery.pk))
        delivery.refresh_from_db()
        self.assertEqual(delivery.last_error_code, 'BOT_IDENTITY_MISMATCH')
        self.assertEqual(delivery.next_attempt_at.year, 9999)
        self.assertEqual(delivery.attempt_count, 0)

    def test_new_delivery_cannot_bypass_shared_bucket_429(self):
        self.assert_shared_gate(False)

    def test_new_delivery_cannot_bypass_global_429(self):
        self.assert_shared_gate(True)

    def assert_shared_gate(self, global_limit):
        self.queue()
        client = Mock(bot_key='offline-test-bot')
        client.send.return_value = SendResult('retry', 'RATE_LIMITED', retry_after=30,
                                               bucket='message/create', global_limit=global_limit)
        self.assertEqual(outbox.process_once(client=client)['outbound'], 1)
        new = self.queue()
        # Fresh client simulates a worker restart; state must live in the DB.
        next_client = Mock(bot_key='offline-test-bot')
        next_client.send.return_value = SendResult('sent', message_id='test-message')
        self.assertEqual(outbox.process_once(client=next_client)['outbound'], 0)
        next_client.send.assert_not_called()
        new.refresh_from_db()
        self.assertEqual(new.attempt_count, 0)
        with patch('django.utils.timezone.now', return_value=timezone.now()+timedelta(seconds=40)):
            outbox.process_once(client=next_client)
        self.assertEqual(next_client.send.call_count, 2)

    def test_gate_created_after_claim_is_checked_immediately_before_http(self):
        self.queue()
        original_fresh = outbox.fresh
        calls = []
        def revalidate(delivery):
            calls.append(delivery.pk)
            if len(calls) == 2:
                outbox.remember_rate_limit('offline-test-bot', SendResult('retry', retry_after=60, global_limit=True))
            return original_fresh(delivery)
        client = Mock(bot_key='offline-test-bot')
        with patch.object(outbox, 'fresh', side_effect=revalidate):
            self.assertEqual(outbox.process_once(client=client)['outbound'], 0)
        client.send.assert_not_called()
        delivery = outbox.KookDelivery.objects.get()
        self.assertEqual(delivery.status, 'queued')
        self.assertEqual(delivery.attempt_count, 0)
        self.assertIsNone(delivery.lease_token)

    def test_shorter_late_response_cannot_reduce_gate_and_bots_are_isolated(self):
        now = timezone.now()
        with patch('django.utils.timezone.now', return_value=now):
            outbox.remember_rate_limit('offline-test-bot', SendResult('retry', retry_after=90, bucket='shared'))
            outbox.remember_rate_limit('offline-test-bot', SendResult('retry', retry_after=10, bucket='shared'))
        self.assertEqual(outbox.rate_blocked_until('offline-test-bot'), now+timedelta(seconds=90))
        self.assertIsNone(outbox.rate_blocked_until('different-bot'))

    def test_real_transport_result_blocks_new_jobs_when_unit_unverified(self):
        self.queue()
        transport = Mock()
        transport.post.return_value = Mock(status_code=429, headers={'X-Rate-Limit-Reset':'14', 'X-Rate-Limit-Global':''})
        client = KookClient(token='synthetic', transport=transport)
        self.assertEqual(outbox.process_once(client=client)['outbound'], 1)
        self.queue()
        self.assertEqual(outbox.process_once(client=client)['outbound'], 0)
        self.assertEqual(transport.post.call_count, 1)

    def test_exhausted_delivery_still_establishes_global_cooldown(self):
        first = self.queue()
        first.attempt_count = 4
        first.save(update_fields=['attempt_count'])
        client = Mock(bot_key='offline-test-bot')
        client.send.return_value = SendResult('retry', 'RATE_LIMITED', retry_after=30, global_limit=True)
        outbox.process_once(client=client)
        self.queue()
        self.assertEqual(outbox.process_once(client=client)['outbound'], 0)
        self.assertEqual(client.send.call_count, 1)


class RateHeaderTests(TestCase):
    def send(self, headers, status=429):
        transport = Mock()
        transport.post.return_value = Mock(status_code=status, headers=headers,
                                           json=lambda: {'code':0, 'data':{'msg_id':'test-id'}})
        return KookClient(token='synthetic', transport=transport).send('channel','test-channel','[]')

    def test_bare_global_header_and_unknown_reset_unit_fail_closed(self):
        result = self.send({'x-rate-limit-global':'', 'x-rate-limit-reset':'14',
                            'x-rate-limit-bucket':'message/create'})
        self.assertTrue(result.global_limit)
        self.assertTrue(result.reset_unverified)
        outbox.remember_rate_limit('offline-test-bot', result)
        self.assertGreater(outbox.rate_blocked_until('offline-test-bot').year, 9000)

    def test_invalid_or_missing_headers_fail_closed_and_http_date_works(self):
        for headers in ({}, {'Retry-After':'NaN'}, {'Retry-After':'-1'}, {'Retry-After':'1e300'},
                        {'Retry-After':'invalid'}, {'X-Rate-Limit-Reset':'NaN'}):
            with self.subTest(headers=headers):
                self.assertTrue(self.send(headers).reset_unverified)
        from email.utils import format_datetime
        now = timezone.now().replace(microsecond=0)
        with patch('django.utils.timezone.now', return_value=now):
            result = self.send({'Retry-After':format_datetime(now+timedelta(seconds=30)), 'X-Rate-Limit-Global':'false'})
        self.assertEqual(result.retry_after, 30)
        self.assertFalse(result.global_limit)
        self.assertFalse(result.reset_unverified)
        self.assertEqual(self.send({'X-Rate-Limit-Remaining':'1'}, status=200).retry_after, 0)

    def test_explicit_units_and_retry_after_and_success_exhaustion(self):
        for unit, delay in (('seconds', 1200), ('milliseconds', 1.2)):
            with override_settings(KOOK_RATE_RESET_UNIT=unit):
                result = self.send({'X-Rate-Limit-Reset':'1200'})
                self.assertAlmostEqual(result.retry_after, delay)
                self.assertFalse(result.reset_unverified)
        result = self.send({'Retry-After':'3', 'X-Rate-Limit-Reset':'1200'})
        self.assertEqual(result.retry_after, 3)
        self.assertFalse(result.reset_unverified)
        result = self.send({'Retry-After':'3', 'X-Rate-Limit-Remaining':'0',
                            'X-Rate-Limit-Bucket':'message/create'}, status=200)
        self.assertEqual(result.status, 'sent')
        self.assertEqual(result.retry_after, 3)
        outbox.remember_rate_limit('offline-test-bot', result)
        self.assertIsNotNone(outbox.rate_blocked_until('offline-test-bot'))
