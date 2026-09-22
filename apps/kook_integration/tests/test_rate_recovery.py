import fcntl
import io
import tempfile
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from apps.kook_integration import outbox
from apps.kook_integration.models import KookOutboxEvent, KookRateLimit, KookAuditLog


@override_settings(KOOK_ENABLED=True, KOOK_SEND_ENABLED=True, KOOK_CHANNEL_SEND_ENABLED=True,
    KOOK_TARGET_CONFIG_VERSION='test', KOOK_VERIFIED_CONFIG_VERSION='test',
    KOOK_VERIFIED_BOT_KEY='offline-test-bot', KOOK_VERIFIED_CHANNEL_ID='test-channel',
    KOOK_VERIFIED_GUILD_ID='test-guild', KOOK_PRODUCTION_ENABLED=True, KOOK_ORDER_EVENTS_ENABLED=True,
    KOOK_ORDER_REVALIDATOR=lambda envelope, delivery: True)
class RecoveryTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='kook-recovery-test-')
        self.addCleanup(self.directory.cleanup)
        self.flags = override_settings(KOOK_WORKER_LOCK_PATH=self.directory.name+'/worker.lock')
        self.flags.enable()
        self.addCleanup(self.flags.disable)
        self.until = datetime.max.replace(tzinfo=dt_timezone.utc)
        self.gate = KookRateLimit.objects.create(bot_key='offline-test-bot', key='global', blocked_until=self.until)

    def queue(self):
        event = KookOutboxEvent.objects.create(event_type='public_slots.opened', source_key=str(uuid.uuid4()),
            order_id=KookOutboxEvent.objects.count()+1, expires_at=timezone.now()+timedelta(minutes=10),
            payload={'audience':'public_channel','safe_snapshot':{}})
        delivery = outbox.schedule_delivery(event, 'channel')
        self.assertIsNone(outbox.claim_delivery(delivery.pk))
        delivery.refresh_from_db()
        self.assertEqual(delivery.next_attempt_at, self.until)
        return delivery

    def recover(self, **kwargs):
        options = dict(bot='offline-test-bot', gate='global', expected_until=self.until.isoformat(),
            config_version='test', actor='operator-test', evidence='CHANGE-123', stdout=io.StringIO())
        options.update(kwargs)
        call_command('recover_kook_rate_limit', **options)
        return options['stdout'].getvalue()

    def execute(self, **kwargs):
        return self.recover(execute=True, confirm='offline-test-bot/global', workers_paused=True, **kwargs)

    def test_manual_hold_records_reason_time_without_raw_response(self):
        from apps.kook_integration.client import SendResult
        self.gate.delete()
        outbox.remember_rate_limit('offline-test-bot', SendResult('retry', reset_unverified=True, global_limit=True))
        audit = KookAuditLog.objects.get(action='rate.hold')
        self.assertEqual(audit.actor, 'offline-test-bot')
        self.assertEqual(audit.result, 'RESET_UNVERIFIED')
        self.assertEqual(audit.request_id, 'global')
        self.assertLessEqual(audit.created_at, timezone.now())

    def test_default_dry_run_then_atomic_audited_recovery(self):
        delivery = self.queue()
        self.assertIn('dry_run', self.recover())
        delivery.refresh_from_db()
        self.assertEqual(delivery.next_attempt_at, self.until)
        self.assertTrue(KookRateLimit.objects.filter(pk=self.gate.pk).exists())
        self.assertFalse(KookAuditLog.objects.filter(action='rate.recover').exists())
        self.execute()
        delivery.refresh_from_db()
        self.assertLessEqual(delivery.next_attempt_at, timezone.now())
        self.assertEqual(delivery.status, 'queued')
        self.assertFalse(KookRateLimit.objects.filter(pk=self.gate.pk).exists())
        audit = KookAuditLog.objects.get(action='rate.recover')
        self.assertEqual(audit.actor, 'operator-test')
        self.assertIn('CHANGE-123', audit.result)
        self.assertEqual(len(audit.request_id), 64)
        scope = KookAuditLog.objects.get(action='rate.recover.scope')
        self.assertEqual(scope.object_id, 'offline-test-bot')
        self.assertEqual(scope.result, 'global')
        self.assertEqual(scope.request_id, audit.request_id)

    def test_invalid_or_stale_deliveries_not_revived_other_gates_not_shortened(self):
        good = self.queue()
        invalid = []
        for status in ('unknown', 'cancelled', 'superseded', 'failed', 'sent'):
            d = self.queue(); d.status = status; d.save(); invalid.append(d)
        for kind in ('expired', 'revision', 'stream_unknown', 'business', 'identity'):
            d = self.queue()
            if kind == 'expired':
                d.event.expires_at = timezone.now()-timedelta(seconds=1); d.event.save()
            elif kind == 'revision':
                d.stream.desired_revision += 1; d.stream.save()
            elif kind == 'stream_unknown':
                d.stream.state = 'unknown'; d.stream.save()
            elif kind == 'identity':
                d.last_error_code = 'BOT_IDENTITY_MISMATCH'; d.save()
            else:
                denied_id = d.pk
            invalid.append(d)
        other_until = timezone.now()+timedelta(seconds=60)
        other = KookRateLimit.objects.create(bot_key='offline-test-bot', key='other', blocked_until=other_until)
        foreign = KookRateLimit.objects.create(bot_key='other-bot', key='global', blocked_until=self.until)
        with override_settings(KOOK_ORDER_REVALIDATOR=lambda envelope, d: d.pk != denied_id):
            self.execute()
        good.refresh_from_db(); self.assertEqual(good.next_attempt_at, other_until)
        for d in invalid:
            status = d.status; d.refresh_from_db()
            self.assertEqual(d.next_attempt_at, self.until)
            self.assertEqual(d.status, status)
        other.refresh_from_db(); foreign.refresh_from_db()
        self.assertEqual(other.blocked_until, other_until)
        self.assertEqual(foreign.blocked_until, self.until)

    def test_confirmation_identity_version_evidence_and_changed_gate_rejected(self):
        for options in ({'execute':True}, {'execute':True, 'workers_paused':True, 'confirm':'wrong'},
                        {'bot':'other-bot'}, {'config_version':'stale'}, {'evidence':''},
                        {'gate':'wrong'}, {'expected_until':timezone.now().isoformat()}):
            with self.subTest(options=options), self.assertRaises(CommandError):
                self.recover(**options)
        self.assertTrue(KookRateLimit.objects.filter(pk=self.gate.pk).exists())

    def test_worker_lock_and_inflight_refuse_recovery(self):
        with open(self.directory.name+'/worker.lock', 'w') as worker:
            fcntl.flock(worker, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesMessage(CommandError, 'KOOK_WORKER_ALREADY_RUNNING'):
                self.execute()
        d = self.queue(); d.status = 'inflight'; d.save()
        with self.assertRaisesMessage(CommandError, 'UNRESOLVED_WORKER_LEASES'):
            self.execute()
        self.assertTrue(KookRateLimit.objects.filter(pk=self.gate.pk).exists())

    def test_audit_failure_rolls_back_gate_and_scheduling(self):
        from unittest.mock import patch
        d = self.queue()
        with patch.object(KookAuditLog.objects, 'create', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.execute()
        d.refresh_from_db()
        self.assertEqual(d.next_attempt_at, self.until)
        self.assertTrue(KookRateLimit.objects.filter(pk=self.gate.pk).exists())
