"""Manual, socket/DB-only recovery; never sends, enables, or guesses Reset units."""
import fcntl
import hashlib
import json
import os
import re
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from apps.kook_integration.models import KookRateLimit, KookDelivery, KookMessageProjection, KookAuditLog
from apps.kook_integration.outbox import fresh
from apps.kook_integration.secrets import bot_key


class Command(BaseCommand):
    help = 'Dry-run by default. Recover one verified KOOK rate gate with workers stopped and locked.'

    def add_arguments(self, parser):
        for name in ('bot', 'gate', 'expected-until', 'config-version', 'actor', 'evidence'):
            parser.add_argument('--'+name, required=True)
        parser.add_argument('--execute', action='store_true')
        parser.add_argument('--confirm', default='')
        parser.add_argument('--workers-paused', action='store_true')

    def handle(self, *args, **options):
        # Evidence is an approved change-record reference, NOT a token/header/body.
        for name, length in (('actor', 100), ('evidence', 40)):
            if not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,%d}' % length, options[name]):
                raise CommandError('INVALID_'+name.upper()+'_REFERENCE')
        if options['bot'] != bot_key():
            raise CommandError('BOT_IDENTITY_MISMATCH')
        version = getattr(settings, 'KOOK_TARGET_CONFIG_VERSION', '')
        if not version or len(version) > 100 or options['config_version'] != version or version != getattr(settings, 'KOOK_VERIFIED_CONFIG_VERSION', ''):
            raise CommandError('CONFIG_VERSION_MISMATCH')
        until = parse_datetime(options['expected_until'])
        if not until or timezone.is_naive(until):
            raise CommandError('EXPECTED_UNTIL_MUST_BE_AWARE')
        if options['execute'] and (not options['workers_paused'] or options['confirm'] != options['bot']+'/'+options['gate']):
            raise CommandError('PAUSE_AND_EXACT_CONFIRMATION_REQUIRED')
        # Exactly the same lock path/inode policy as process_kook_outbox. No CLI bypass.
        path = getattr(settings, 'KOOK_WORKER_LOCK_PATH', '/run/lock/huc125-kook-worker.lock')
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise CommandError('WORKER_LOCK_UNAVAILABLE')
        with os.fdopen(descriptor, 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CommandError('KOOK_WORKER_ALREADY_RUNNING')
            with transaction.atomic():
                self.recover(options, until)

    def recover(self, options, until):
        key = options['bot']
        gates = list(KookRateLimit.objects.select_for_update().filter(bot_key=key).order_by('pk'))
        gate = next((g for g in gates if g.key == options['gate']), None)
        if gate is None or gate.blocked_until != until:
            raise CommandError('GATE_NOT_FOUND_OR_CHANGED')
        now = timezone.now()
        if (KookDelivery.objects.filter(stream__bot_key=key, status='inflight').exists() or
                KookMessageProjection.objects.filter(bot_key=key, lease_token__isnull=False).exists()):
            raise CommandError('UNRESOLVED_WORKER_LEASES')
        remaining = max([g.blocked_until for g in gates if g.pk != gate.pk and g.blocked_until > now] or [now])
        queued = KookDelivery.objects.filter(stream__bot_key=key, status='queued')
        oldest = queued.order_by('event__occurred_at').values_list('event__occurred_at', flat=True).first()
        hold = KookAuditLog.objects.filter(action='rate.hold', actor=key, object_id=str(gate.pk)).order_by('-created_at').first()
        report = dict(hold_reason=hold.result if hold else 'LEGACY_REASON_UNRECORDED',
            hold_recorded_at=hold.created_at.isoformat() if hold else None, mode='execute' if options['execute'] else 'dry_run', bot=key, gate=gate.key,
            gate_id=gate.pk, blocked_until=until.isoformat(),
            kind='manual_hold' if until.year == 9999 else 'finite_cooldown',
            queued=queued.count(), oldest_event_age_seconds=max(0, (now-oldest).total_seconds()) if oldest else None,
            remaining_until=remaining.isoformat(), eligible=0, skipped=0, evidence=options['evidence'])
        # Match the exact persisted gate deadline: do not shorten unrelated retries.
        for delivery in queued.select_for_update().filter(next_attempt_at=until).select_related('event').order_by('pk'):
            stream = KookMessageProjection.objects.select_for_update().get(pk=delivery.stream_id)
            delivery.stream = stream
            if (stream.state == 'unknown' or stream.lease_token or
                    delivery.last_error_code == 'BOT_IDENTITY_MISMATCH' or
                    delivery.event.revision != stream.desired_revision or
                    delivery.event.revision <= stream.applied_revision):
                report['skipped'] += 1
                continue
            allowed, _ = fresh(delivery)  # Includes expiry, consent, config, current business facts.
            if not allowed:
                report['skipped'] += 1
                continue
            report['eligible'] += 1
            if options['execute']:
                delivery.next_attempt_at = remaining
                delivery.save(update_fields=['next_attempt_at'])
        if options['execute']:
            # Same transaction: an audit failure rolls back both clearing and rescheduling.
            proof = dict(report, actor=options['actor'], config_version=options['config_version'])
            audit_hash = hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()
            KookAuditLog.objects.create(actor=options['actor'], action='rate.recover',
                object_id=str(gate.pk), request_id=audit_hash,
                result=options['evidence']+':released='+str(report['eligible']))
            for action, object_id, result in (
                ('rate.recover.scope', key, gate.key),
                ('rate.recover.window', str(gate.pk), until.isoformat()),
                ('rate.recover.config', options['config_version'], options['evidence']),
            ):
                KookAuditLog.objects.create(actor=options['actor'], action=action,
                    object_id=object_id, request_id=audit_hash, result=result)
            gate.delete()
            report['audit_sha256'] = audit_hash
        self.stdout.write(json.dumps(report, sort_keys=True))
