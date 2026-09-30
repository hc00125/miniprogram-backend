"""Persistent outbox primitives; business facts are supplied by the orders owner.

Single-worker deployment is mandatory until production-engine concurrency QA.
No network while holding the claim transaction. Expired sends become unknown.
"""
import hashlib
import json
import random
import uuid
from datetime import datetime, timedelta, timezone as datetime_timezone
from django.conf import settings
from django.db import transaction, connection
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.module_loading import import_string
from apps.players.models import Player
from .models import KookOutboxEvent, KookDelivery, KookMessageProjection, KookBinding, KookInboundEvent, KookAuditLog, KookRateLimit
from .contracts import validate_envelope
from .secrets import KookError, bot_key, require_enabled
from .binding import digest, claim_code
from .client import KookClient
from .rendering import render

LEASE_SECONDS=60


def rate_blocked_until(key):
    # Persistent bot-scoped gate covers *new* jobs and all operation/target routes.
    return KookRateLimit.objects.filter(bot_key=key, blocked_until__gt=timezone.now()).order_by('-blocked_until').values_list('blocked_until', flat=True).first()


@transaction.atomic
def remember_rate_limit(key, result):
    if result.status != 'retry' and not result.retry_after and not result.reset_unverified:
        return
    until = (datetime.max.replace(tzinfo=datetime_timezone.utc) if result.reset_unverified
             else timezone.now() + timedelta(seconds=max(1, result.retry_after)))
    gate_key = 'global' if result.global_limit or not result.bucket else hashlib.sha256(result.bucket.encode()).hexdigest()
    gate, created = KookRateLimit.objects.get_or_create(bot_key=key, key=gate_key, defaults={'blocked_until': until})
    # Conditional UPDATE prevents a shorter late response from reducing a deadline.
    extended = KookRateLimit.objects.filter(pk=gate.pk, blocked_until__lt=until).update(blocked_until=until)
    if created or extended:
        KookAuditLog.objects.create(actor=key, action='rate.hold', object_id=str(gate.pk),
            request_id=gate_key, result='RESET_UNVERIFIED' if result.reset_unverified else 'COOLDOWN')


def append_event(envelope):
    if not connection.in_atomic_block:
        raise RuntimeError('append_event requires business transaction.atomic')
    data=validate_envelope(envelope)
    fields={k:data[k] for k in ['event_id','schema_version','event_type','order_id','revision']}
    fields.update(designation_id=data.get('designation_id'),occurred_at=parse_datetime(data['occurred_at']),expires_at=parse_datetime(data['expires_at']),payload=data)
    event,created=KookOutboxEvent.objects.get_or_create(source_key=data['source_key'],defaults=fields)
    if not created:
        previous=dict(event.payload);incoming=dict(data)
        for item in ['event_id','occurred_at']:
            previous.pop(item,None);incoming.pop(item,None)
        if previous!=incoming:
            raise KookError('SOURCE_KEY_CONFLICT')
    return event

@transaction.atomic
def schedule_delivery(event, scope, binding=None, operation=None):
    """B calls in same business transaction as append_event. Targets not caller selectable."""
    if scope=='dm':
        if not binding or not binding.active or binding.bot_key!=bot_key():
            raise KookError('INVALID_BINDING')
        if event.event_type!='test.dm' and event.payload.get('audience')!='designated_dm':
            raise ValueError('Audience mismatch')
        target=binding.kook_user_id
    elif scope=='channel':
        if event.payload.get('audience')!='public_channel':
            raise ValueError('Private event cannot become public')
        target=getattr(settings,'KOOK_VERIFIED_CHANNEL_ID','')
        if not target:
            raise KookError('TARGET_NOT_VERIFIED',503)
    else:
        raise ValueError('Invalid scope')
    stream,_=KookMessageProjection.objects.get_or_create(bot_key=bot_key(),scope=scope,target_id=target,order_id=event.order_id or 0,designation_key=str(event.designation_id or (event.event_id if event.event_type=='test.dm' else '')),public_epoch=event.payload.get('public_epoch',0))
    stream=KookMessageProjection.objects.select_for_update().get(pk=stream.pk)
    if event.revision>stream.desired_revision:
        stream.desired_revision=event.revision
        stream.save(update_fields=['desired_revision'])
    operation=operation or ('update' if stream.remote_message_id else 'create')
    if operation not in {'create','update'}:
        raise ValueError('Invalid operation')
    delivery,_=KookDelivery.objects.get_or_create(event=event,scope=scope,target_id=target,operation=operation,defaults={'binding':binding,'binding_version':binding.version if binding else None,'stream':stream})
    return delivery

def send_allowed(binding=None,scope='dm'):
    if not getattr(settings,'KOOK_ENABLED',False) or not getattr(settings,'KOOK_SEND_ENABLED',False):
        return False
    if scope=='dm':
        return bool(getattr(settings,'KOOK_DM_SEND_ENABLED',False) and binding and binding.active and binding.notifications_enabled and binding.bot_key==bot_key() and binding.kook_user_id in getattr(settings,'KOOK_DM_ALLOWLIST',[]) and binding.player_id in getattr(settings,'KOOK_PLAYER_ALLOWLIST',[]))
    version=getattr(settings,'KOOK_TARGET_CONFIG_VERSION','')
    return bool(getattr(settings,'KOOK_CHANNEL_SEND_ENABLED',False) and version and version==getattr(settings,'KOOK_VERIFIED_CONFIG_VERSION',None) and getattr(settings,'KOOK_VERIFIED_BOT_KEY','')==bot_key() and getattr(settings,'KOOK_VERIFIED_CHANNEL_ID','') and getattr(settings,'KOOK_VERIFIED_GUILD_ID',''))

@transaction.atomic
def enqueue_test(player,request_id,ip):
    Player.objects.select_for_update().get(pk=player.pk)
    source='test.dm:'+bot_key()+':'+str(player.pk)+':'+str(request_id)
    existing=KookDelivery.objects.filter(event__source_key=source,binding__player=player).first()
    if existing:
        return existing
    binding=KookBinding.objects.filter(player=player,bot_key=bot_key(),active=True).first()
    if not send_allowed(binding):
        raise KookError('KOOK_SEND_UNAVAILABLE',503)
    ipkey=digest('ip:'+ip)
    # Player locks protect per-user limits, not a shared IP across users.
    # Transaction-scoped PG lock makes the IP count + audit insert atomic.
    # SQLite remains single-writer/offline only; no multiworker guarantee.
    if connection.vendor == 'postgresql':
        lock_key=int.from_bytes(hashlib.sha256(('kook:test-ip:'+ipkey).encode()).digest()[:8], 'big', signed=True)
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', [lock_key])
    now=timezone.now()
    recent=KookAuditLog.objects.filter(actor=str(player.pk),action='test.enqueue')
    if recent.filter(created_at__gte=now-timedelta(minutes=1)).exists() or recent.filter(created_at__gte=now-timedelta(days=1)).count()>=5 or KookAuditLog.objects.filter(action='test.enqueue',object_id=ipkey,created_at__gte=now-timedelta(minutes=1)).count()>=5:
        raise KookError('TEST_RATE_LIMIT',429)
    event=KookOutboxEvent.objects.create(event_type='test.dm',source_key=source,expires_at=now+timedelta(minutes=10),payload={})
    delivery=schedule_delivery(event,'dm',binding)
    KookAuditLog.objects.create(actor=str(player.pk),action='test.enqueue',object_id=ipkey,request_id=str(request_id),result='queued')
    return delivery

def process_inbound(limit):
    # Atomic proof claim and payload erasure in one transaction: crash rolls back.
    processed=0
    for pk in KookInboundEvent.objects.filter(bot_key=bot_key(),state='queued').order_by('pk').values_list('pk',flat=True)[:limit]:
        with transaction.atomic():
            event=KookInboundEvent.objects.select_for_update().get(pk=pk)
            if event.state!='queued':
                continue
            data=event.payload
            actor=digest('author:'+data.get('author_id',''))
            recent=KookAuditLog.objects.filter(action='binding.proof',created_at__gte=timezone.now()-timedelta(minutes=1))
            permitted=recent.count()<1000 and recent.filter(actor=actor).count()<10
            success=permitted and event.received_at>timezone.now()-timedelta(minutes=10) and claim_code(data.get('code',''),data.get('author_id',''),event.event_key,data.get('display_name',''))
            KookAuditLog.objects.create(actor=actor,action='binding.proof',result='candidate' if success else 'ignored')
            event.state='processed';event.payload={};event.attempt_count+=1
            event.save(update_fields=['state','payload','attempt_count'])
            processed+=1
    return processed

def identity_matches(delivery, client_key=None):
    key = bot_key()
    return bool(delivery.stream_id and delivery.stream.bot_key == key and
                (client_key is None or client_key == key) and
                KookMessageProjection.objects.filter(pk=delivery.stream_id, bot_key=key).exists())

def fresh(delivery):
    if not identity_matches(delivery):
        return False, 'BOT_IDENTITY_MISMATCH'
    event=delivery.event
    if event.expires_at<=timezone.now():
        return False,'EXPIRED'
    if delivery.binding_id:
        binding=KookBinding.objects.select_related('player__user').filter(pk=delivery.binding_id).first()
        if not binding or not binding.active or binding.version!=delivery.binding_version or not binding.notifications_enabled or binding.kook_user_id!=delivery.target_id:
            return False,'BINDING_REVOKED'
        if binding.player.status!='approved':
            return False,'PLAYER_NOT_APPROVED'
        profile=getattr(binding.player.user,'client_profile',None) if binding.player.user else None
        if not profile or not profile.openid or not binding.player.user.is_active or profile.account_status!='active':
            return False,'ACCOUNT_RESTRICTED'
        if not send_allowed(binding):
            return False,'SEND_DISABLED'
    elif not send_allowed(scope='channel') or delivery.target_id!=getattr(settings,'KOOK_VERIFIED_CHANNEL_ID',''):
        return False,'SEND_DISABLED'
    if event.event_type!='test.dm':
        if not getattr(settings,'KOOK_ORDER_EVENTS_ENABLED',False):
            return False,'ORDER_EVENTS_DISABLED'
        if not getattr(settings,'KOOK_PRODUCTION_ENABLED',False) and event.order_id not in getattr(settings,'KOOK_TEST_ORDER_IDS',[]):
            return False,'ORDER_NOT_ALLOWLISTED'
        hook=getattr(settings,'KOOK_ORDER_REVALIDATOR','')
        if not hook:
            return False,'ORDER_REVALIDATOR_MISSING'
        validator=import_string(hook) if isinstance(hook,str) else hook
        if validator(event.payload, delivery) is not True:
            return False,'BUSINESS_STATE_CHANGED'
    return True,''

@transaction.atomic
def claim_delivery(pk):
    delivery=KookDelivery.objects.select_for_update().select_related('event').get(pk=pk)
    if delivery.status!='queued' or delivery.next_attempt_at>timezone.now():
        return None
    if not delivery.stream_id:
        delivery.status='failed';delivery.last_error_code='STREAM_MISSING';delivery.save();return None
    stream=KookMessageProjection.objects.select_for_update().get(pk=delivery.stream_id)
    delivery.stream = stream
    if not identity_matches(delivery):
        delivery.last_error_code = 'BOT_IDENTITY_MISMATCH'
        delivery.next_attempt_at = datetime.max.replace(tzinfo=datetime_timezone.utc)
        delivery.save(update_fields=['last_error_code', 'next_attempt_at'])
        return None
    blocked = rate_blocked_until(stream.bot_key)
    if blocked:
        delivery.next_attempt_at = blocked
        delivery.save(update_fields=['next_attempt_at'])
        return None
    if stream.state=='unknown' or (stream.lease_until and stream.lease_until>timezone.now()):
        return None
    if delivery.event.revision<stream.desired_revision or delivery.event.revision<=stream.applied_revision:
        delivery.status='superseded';delivery.save(update_fields=['status']);return None
    allowed,reason=fresh(delivery)
    if not allowed:
        delivery.status='cancelled';delivery.last_error_code=reason;delivery.save(update_fields=['status','last_error_code']);return None
    if stream.remote_message_id:
        # Use the same persisted message, even if queued while its create ran.
        delivery.operation='update';delivery.remote_message_id=stream.remote_message_id
    elif delivery.operation=='update':
        delivery.status='failed';delivery.last_error_code='MESSAGE_ID_MISSING';delivery.save();return None
    token=uuid.uuid4();until=timezone.now()+timedelta(seconds=LEASE_SECONDS)
    stream.lease_token=token;stream.lease_until=until;stream.save(update_fields=['lease_token','lease_until'])
    delivery.lease_token=token;delivery.lease_until=until;delivery.status='inflight';delivery.attempt_count+=1
    delivery.save()
    return delivery

@transaction.atomic
def finish_delivery(delivery,result):
    # Even exhausted/late deliveries must not discard authoritative 429 state.
    remember_rate_limit(delivery.stream.bot_key, result)
    current=KookDelivery.objects.select_for_update().filter(pk=delivery.pk,status='inflight',lease_token=delivery.lease_token).first()
    if not current:
        return
    stream=KookMessageProjection.objects.select_for_update().get(pk=current.stream_id)
    if stream.lease_token!=delivery.lease_token:
        return
    current.last_error_code=result.error_code
    if result.status=='sent':
        current.status='sent';current.remote_message_id=result.message_id;current.sent_at=timezone.now()
        stream.remote_message_id=result.message_id;stream.applied_revision=current.event.revision;stream.state='sent'
    elif result.status=='deferred':
        current.status='queued'
        current.next_attempt_at=(datetime.max.replace(tzinfo=datetime_timezone.utc)
                                 if result.error_code == 'BOT_IDENTITY_MISMATCH'
                                 else rate_blocked_until(stream.bot_key) or timezone.now())
        current.attempt_count=max(0, current.attempt_count-1)
    elif result.status=='retry' and current.attempt_count<5:
        delay=max(result.retry_after,2**current.attempt_count+random.random())
        current.next_attempt_at=max(timezone.now()+timedelta(seconds=delay), rate_blocked_until(stream.bot_key) or timezone.now())
        current.status='queued' if current.next_attempt_at<current.event.expires_at else 'cancelled'
    else:
        current.status='unknown' if result.status=='unknown' else 'failed'
        if current.status=='unknown':
            stream.state='unknown'
    current.lease_token=None;current.lease_until=None
    stream.lease_token=None;stream.lease_until=None
    current.save();stream.save()

@transaction.atomic
def recover_expired_leases():
    for d in KookDelivery.objects.select_for_update().filter(status='inflight',lease_until__lte=timezone.now()):
        d.status='unknown';d.last_error_code='LEASE_EXPIRED_UNKNOWN';d.save(update_fields=['status','last_error_code'])
        KookMessageProjection.objects.filter(pk=d.stream_id,lease_token=d.lease_token).update(state='unknown',lease_until=None,lease_token=None)

def process_once(batch_size=50,client=None):
    require_enabled()
    inbound=process_inbound(batch_size)
    recover_expired_leases()
    if not getattr(settings,'KOOK_SEND_ENABLED',False):
        return {'inbound':inbound,'outbound':0}
    client=client or KookClient()
    count=0
    if not isinstance(getattr(client, 'bot_key', None), str) or client.bot_key != bot_key():
        return {'inbound':inbound,'outbound':0}
    # Explicitly park foreign streams, never relabel or migrate their messages.
    KookDelivery.objects.filter(status='queued', stream__isnull=False).exclude(stream__bot_key=bot_key()).update(
        last_error_code='BOT_IDENTITY_MISMATCH', next_attempt_at=datetime.max.replace(tzinfo=datetime_timezone.utc))
    ids=list(KookDelivery.objects.filter(status='queued',stream__bot_key=client.bot_key,next_attempt_at__lte=timezone.now()).order_by('next_attempt_at').values_list('pk',flat=True)[:batch_size])
    for pk in ids:
        delivery=claim_delivery(pk)
        if not delivery:
            continue
        # One last binding/fact check after the short claim transaction.
        allowed,reason=fresh(delivery)
        if not allowed:
            from .client import SendResult
            finish_delivery(delivery,SendResult('deferred' if reason == 'BOT_IDENTITY_MISMATCH' else 'failed',reason));continue
        first_public = delivery.event.event_type == 'public_slots.opened' and delivery.event.payload.get('public_epoch') == 0
        mention_policy = ('KOOK_MENTION_FIRST_PUBLIC_ENABLED' if first_public else 'KOOK_MENTION_REOPEN_ENABLED')
        mention = bool(delivery.scope == 'channel' and delivery.operation == 'create'
            and delivery.attempt_count == 1
            and delivery.event.event_type in {'public_slots.opened', 'replacement.opened'}
            and getattr(settings, 'KOOK_PRODUCTION_ENABLED', False)
            and getattr(settings, 'KOOK_MENTION_ALL_VERIFIED', False)
            and getattr(settings, mention_policy, False))
        from .navigation import delivery_url_link
        url_link = delivery_url_link(delivery)
        # WeChat generation may take time: recheck order/binding after it too.
        allowed, reason = fresh(delivery)
        if not allowed:
            from .client import SendResult
            finish_delivery(delivery, SendResult('deferred' if reason == 'BOT_IDENTITY_MISMATCH' else 'failed', reason));continue
        content=render(delivery.event,mention_all=mention,url_link=url_link)
        KookDelivery.objects.filter(pk=pk,lease_token=delivery.lease_token,status='inflight').update(payload_hash=hashlib.sha256(content.encode()).hexdigest())
        if not identity_matches(delivery, client.bot_key):
            from .client import SendResult
            finish_delivery(delivery, SendResult('deferred', 'BOT_IDENTITY_MISMATCH'))
            continue
        if rate_blocked_until(client.bot_key):
            from .client import SendResult
            finish_delivery(delivery, SendResult('deferred', 'RATE_LIMITED'))
            continue
        result=client.send(delivery.scope,delivery.target_id,content,operation=delivery.operation,message_id=delivery.remote_message_id)
        finish_delivery(delivery,result)
        count+=1
    return {'inbound':inbound,'outbound':count}
