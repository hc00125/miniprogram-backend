"""Transactional, network-free KOOK order adapter. Opt-in; no payment rules.

Callbacks are deliberately not configured here. I owns settings wiring.
"""
import hashlib
import json
import uuid
from datetime import timedelta
from django.conf import settings
from django.db import connection
from django.utils import timezone
from .models import Order

SYSTEM_NOTE_PREFIXES = ('规格：', '基础规格：', '指定陪玩：', '指定陪玩等级计价：', '指定后预计价格：', '购买数量：', '合并结算商品：')


def enabled():
    return bool(getattr(settings, 'KOOK_ENABLED', False) and getattr(settings, 'KOOK_ORDER_EVENTS_ENABLED', False))


def manual_note(value):
    # Same line-prefix rule as the actual grab page. Never scrub phone patterns.
    return '\n'.join(line.strip() for line in (value or '').splitlines()
                     if line.strip() and not line.strip().startswith(SYSTEM_NOTE_PREFIXES))[:2000] or '无'


def public_open(order):
    from .designations import remaining_public_slots
    from .replacement_fixes import open_public_replacement
    if order.order_type != Order.ORDER_TYPE_NORMAL or order.fulfillment_mode != 'public':
        return False
    if order.status in {Order.STATUS_CANCELLED, Order.STATUS_COMPLETED}:
        return False
    if not order.paid and order.created_at + timedelta(minutes=120) <= timezone.now():
        return False
    return bool((order.status == Order.STATUS_WAITING or open_public_replacement(order)) and remaining_public_slots(order) > 0)


def public_snapshot(order):
    from apps.catalog.models import PlayerType
    ids = [x.get('type_id') for x in order.designated_types or [] if x.get('type_id')]
    names = dict(PlayerType.objects.filter(pk__in=ids).values_list('pk', 'name'))
    return {
        'published_at': timezone.localtime(order.created_at).strftime('%Y-%m-%d %H:%M'),
        'participant_count': order.order_players.count(),
        'player_type_label': '、'.join(dict.fromkeys(names[x] for x in ids if x in names)) or ('指定类型' if ids else '不限类型'),
        'boss_note': manual_note(order.boss_note),
        'status': order.status,
        'slot_summary': [f'{order.order_players.count()}/{order.required_players}人'],
    }


def public_order_in_scope(order):
    """Fixed deployment boundary; missing/invalid/naive values deny new streams."""
    from django.utils.dateparse import parse_datetime
    raw = getattr(settings, 'KOOK_ORDER_PUBLIC_CREATED_SINCE', '')
    try:
        since = parse_datetime(raw) if isinstance(raw, str) else None
    except ValueError:
        return False
    return bool(since and timezone.is_aware(since) and order.created_at >= since)


def scoped_dm_binding(row):
    """Shared capture/send DM authorization in every notification scope."""
    from django.utils.dateparse import parse_datetime
    from apps.kook_integration.models import KookBinding
    from apps.kook_integration.outbox import send_allowed
    from apps.kook_integration.secrets import bot_key
    raw = getattr(settings, 'KOOK_ORDER_DM_INVITATIONS_SINCE', '')
    try:
        since = parse_datetime(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    if not since or timezone.is_naive(since) or row.invited_at < since:
        return None
    binding = KookBinding.objects.filter(bot_key=bot_key(), player_id=row.player_id,
        active=True, notifications_enabled=True).first()
    if not send_allowed(binding):
        return None
    from apps.players.escort_qualification import escort_order_block_reason
    player = eligible_player(row.player_id)
    if not player or not player.can_be_designated or escort_order_block_reason(row.order, player):
        return None
    return binding


def record_order_state(order_id, cause='state', source_key=None, designation_id=None):
    """Persist final facts inside the caller's transaction; unchanged facts no-op.

    source_key is retained for API compatibility; revision + canonical state is
    the stable dedup key, never a per-callback random identifier.
    """
    if not enabled():
        return []
    # Canary capture must not persist customer facts outside explicit test IDs.
    # Empty allowlists deny all; only a separately approved production gate bypasses.
    if not getattr(settings, 'KOOK_PRODUCTION_ENABLED', False) and order_id not in getattr(settings, 'KOOK_TEST_ORDER_IDS', []):
        return []
    if not connection.in_atomic_block:
        raise RuntimeError('KOOK order facts require the business transaction')
    from apps.kook_integration.models import KookOrderProjection
    from apps.kook_integration.outbox import append_event, schedule_delivery
    from apps.kook_integration.secrets import bot_key
    order = Order.objects.select_for_update().get(pk=order_id)
    scope = getattr(settings, 'KOOK_ORDER_NOTIFICATION_SCOPE', 'all')
    if scope not in {'all', 'designated_dm'}:
        return []
    dm_only = scope == 'designated_dm'
    # Public scope adds only public facts; it never expands DM authorization.
    rows = [row for row in order.designations.all() if scoped_dm_binding(row)]
    if dm_only and (not rows or (order.fulfillment_mode == 'targeted' and not order.paid)):
        return []
    public_allowed = not dm_only and public_order_in_scope(order)
    if not public_allowed and not rows:
        from apps.kook_integration.models import KookMessageProjection
        if not KookMessageProjection.objects.filter(bot_key=bot_key(), order_id=order.pk,
                scope='channel').exclude(remote_message_id='').exists():
            return []
    projection, _ = KookOrderProjection.objects.get_or_create(order=order)
    before = projection.latest_safe_snapshot
    snapshot = public_snapshot(order)
    # Keep an existing round's closure obligation, never grant an old new round.
    opened = not dm_only and public_open(order) and (public_allowed or before.get('public_open', False))
    invitations = {}
    if order.fulfillment_mode != 'targeted' or order.paid:
        for row in rows:
            active = (row.status == 'pending' and row.expires_at > timezone.now()
                      and order.status in {Order.STATUS_WAITING, Order.STATUS_IN_PROGRESS, Order.STATUS_READY_TO_START})
            invitations[str(row.pk)] = {'active': active, 'status': row.status,
                'expires_at': row.expires_at.isoformat(), 'player_id': row.player_id}
    from .replacement_fixes import open_public_replacement
    replacement = open_public_replacement(order)
    from .cancellation_models import OrderReplacementState
    replacement_status = OrderReplacementState.objects.filter(order=order).values_list('status', flat=True).first()
    # Restoration is not a new mention entitlement. Persist it for the whole
    # round so coalescing/edits before first send cannot resurrect an opened @.
    silent_round = before.get('public_silent_round', False)
    if opened and not before.get('public_open'):
        silent_round = (before.get('replacement_status') == 'cancel_requested' or cause == 'revoke_cancel_remaining')
    state = {'public_silent_round': silent_round, 'replacement_status': replacement_status,
             'public_open': opened, 'public_ever_open': opened or before.get('public_ever_open', before.get('public_open', False)),
             'public': snapshot, 'paid': order.paid,
             'mode': order.fulfillment_mode, 'invitations': invitations,
             'replacement': bool(replacement),
             'replacement_type': replacement.required_player_type_id if replacement else None}
    canonical = hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if canonical == projection.canonical_state_hash:
        return []
    # Only a persisted closed -> available transition starts a new public round.
    # Revision-only edits and duplicate callbacks never increment the epoch.
    if opened and not before.get('public_open') and before.get('public_ever_open', False):
        projection.public_epoch += 1
    projection.revision += 1
    projection.canonical_state_hash = canonical
    projection.latest_safe_snapshot = state
    projection.save()
    events = []
    # Cancellation after a full/closed round still edits its known card. Never
    # create a new card, revisit older epochs, or notify execution/settlement.
    cancel_existing = False
    if (not dm_only and not opened and order.status == Order.STATUS_CANCELLED
            and before.get('public', {}).get('status') != Order.STATUS_CANCELLED):
        from apps.kook_integration.models import KookMessageProjection
        cancel_existing = KookMessageProjection.objects.filter(bot_key=bot_key(),
            order_id=order.pk, scope='channel', public_epoch=projection.public_epoch,
            target_id=getattr(settings, 'KOOK_VERIFIED_CHANNEL_ID', '')).exclude(remote_message_id='').exists()
    if not dm_only and (public_allowed or not opened) and (opened or before.get('public_open') or cancel_existing):
        kind = 'public_slots.changed' if opened and before.get('public_open') else 'public_slots.opened' if opened else 'order.cancelled' if order.status == Order.STATUS_CANCELLED else 'public_slots.closed'
        if kind == 'public_slots.changed':
            from apps.kook_integration.models import KookMessageProjection
            has_message = KookMessageProjection.objects.filter(bot_key=bot_key(), order_id=order.pk,
                scope='channel', public_epoch=projection.public_epoch).exclude(remote_message_id='').exists()
            if not has_message:
                # Coalescing must not discard the first-public mention intent.
                # Inflight/unknown streams remain fenced by A; a successful
                # inflight create turns this into an update before rendering.
                kind = 'public_slots.opened'
        if kind == 'public_slots.opened' and replacement:
            kind = 'replacement.opened'
        elif kind == 'public_slots.closed' and before.get('replacement'):
            kind = 'replacement.resolved'
        if opened and silent_round:
            # Existing v1 changed + create contract is deliberately non-mention.
            kind = 'public_slots.changed'
        # Snapshot status describes this notification opportunity, not execution
        # progress. Keep the canonical business snapshot unchanged for dedup.
        event_snapshot = dict(snapshot)
        if not opened:
            # Freeze this round's facts: a delayed old-epoch close must never
            # borrow the current order/new round's status during rendering.
            reason = ('已取消' if order.status == Order.STATUS_CANCELLED else
                      '已接满' if order.required_players > 0 and snapshot['participant_count'] >= order.required_players else
                      '补位已结束' if before.get('replacement') and replacement_status in {'resolved', 'cancel_requested'} else
                      '已关闭')
            event_snapshot['status'] = f'{reason} · 不可接单'
        now = timezone.now()
        event = append_event(dict(schema_version=1, event_id=str(uuid.uuid4()), event_type=kind,
            source_key=f'orders:{bot_key()}:{order.pk}:{projection.revision}:public',
            order_id=order.pk, designation_id=None, revision=projection.revision,
            occurred_at=now.isoformat(), expires_at=(now + timedelta(days=1)).isoformat(),
            fulfillment_mode=order.fulfillment_mode, paid=order.paid,
            audience='public_channel', public_epoch=projection.public_epoch, safe_snapshot=event_snapshot))
        events.append(event.event_id)
        # Persist events even before target verification. No historic replay job.
        if getattr(settings, 'KOOK_VERIFIED_CHANNEL_ID', ''):
            schedule_delivery(event, 'channel', operation=None if opened else 'update')
    from decimal import Decimal
    from apps.kook_integration.models import KookBinding
    for key, facts in invitations.items():
        old = before.get('invitations', {}).get(key, {})
        if not facts['active'] and not old.get('active'):
            continue
        if facts == old and snapshot == before.get('public'):
            continue
        now = timezone.now()
        snap = dict(snapshot)
        # Actual invitation page: order diamonds, not individual wages.
        amount = Decimal(str(order.total_amount or order.total_price_per_hour or 0)) * Decimal('10')
        invitation_status = '待接受指定' if facts['active'] else {
            'accepted': '邀请已接受', 'declined': '邀请已拒绝',
            'expired': '邀请已过期', 'cancelled': '邀请已取消',
        }.get(facts['status'], '邀请已取消' if order.status == Order.STATUS_CANCELLED else '邀请已关闭')
        snap.update(package_label=order.package_name_snapshot or order.package.name,
                    duration_label=f'{order.booked_hours or 1:g}小时',
                    price_label=f'{amount.normalize():f}钻石', deadline_label=facts['expires_at'],
                    status=invitation_status)
        expiry = timezone.datetime.fromisoformat(facts['expires_at']) if facts['active'] else now + timedelta(days=1)
        event = append_event(dict(schema_version=1, event_id=str(uuid.uuid4()),
            event_type='designation.created' if facts['active'] else 'designation.closed',
            source_key=f'orders:{bot_key()}:{order.pk}:{projection.revision}:dm:{key}',
            order_id=order.pk, designation_id=int(key), revision=projection.revision,
            occurred_at=now.isoformat(), expires_at=expiry.isoformat(),
            fulfillment_mode=order.fulfillment_mode, paid=order.paid,
            audience='designated_dm', public_epoch=0, safe_snapshot=snap))
        events.append(event.event_id)
        binding = KookBinding.objects.filter(bot_key=bot_key(), player_id=facts['player_id'],
                                             active=True, notifications_enabled=True).first()
        if binding:
            schedule_delivery(event, 'dm', binding=binding, operation=None if facts['active'] else 'update')
    return events


# Entry decorators live on concrete source functions, before AppConfig.ready()
# captures aliases. Nested mutations publish only the outermost final snapshot.
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from django.db import transaction
_pending = ContextVar('kook_order_mutations', default=None)


def order_event_boundary(function):
    params = signature(function)

    @wraps(function)
    def wrapped(*args, **kwargs):
        if not enabled():
            return function(*args, **kwargs)
        bound = params.bind(*args, **kwargs).arguments
        outer = _pending.get() is None
        token = _pending.set(set()) if outer else None
        ids = _pending.get()

        def collect(value):
            if isinstance(value, Order):
                ids.add(value.pk)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    collect(item)
            elif isinstance(value, dict):
                for item in value.values():
                    collect(item)
            elif getattr(value, 'order_id', None):
                # Payment/Refund.order uses to_field=order_no, not the PK.
                collect(value.order)

        try:
            with transaction.atomic():
                collect(bound.get('order'))
                if bound.get('order_no'):
                    ids.update(Order.objects.filter(order_no=bound['order_no']).values_list('pk', flat=True))
                if function.__name__ in {'expire_due_designations', 'expire_due_replacement_designations'}:
                    from .models import OrderDesignation
                    due = OrderDesignation.objects.filter(status='pending', expires_at__lte=bound.get('now') or timezone.now())
                    if bound.get('order'):
                        due = due.filter(order=bound['order'])
                    ids.update(due.values_list('order_id', flat=True))
                result = function(*args, **kwargs)
                collect(result)
                if getattr(result, 'status_code', 200) >= 400:
                    return result
                if outer:
                    for pk in sorted(ids):
                        record_order_state(pk, cause=function.__name__)
                return result
        finally:
            if outer:
                _pending.reset(token)
    return wrapped


def eligible_player(player_id):
    from apps.players.models import Player
    from .discipline import discipline_block_reason
    player = Player.objects.select_related('user', 'player_type', 'user__client_profile').filter(pk=player_id).first()
    if not player or player.status != 'approved' or not player.can_accept_orders or not player.user_id:
        return None
    profile = getattr(player.user, 'client_profile', None)
    if not player.user.is_active or not profile or not profile.openid or profile.account_status != 'active':
        return None
    if discipline_block_reason(player):
        return None
    return player


def resolve_entry(intent, player):
    """Pure business resolver; opaque token and binding checks also live in A."""
    from .models import OrderDesignation
    from .replacement_fixes import open_public_replacement, can_player_take_replacement
    from .services import can_player_grab_order_readonly
    from .room_entry_requeue import can_player_grab_after_room_timeout
    from .discipline import can_player_grab_with_discipline
    from apps.players.escort_qualification import escort_order_block_reason
    from apps.kook_integration.models import KookBinding
    from apps.kook_integration.secrets import bot_key
    forbidden = {'state': 'forbidden'}
    if not player or not getattr(player, 'pk', None):
        return forbidden
    player = eligible_player(player.pk)
    if not player:
        return forbidden
    if intent.revoked_at or intent.expires_at <= timezone.now():
        return {'state': 'expired'}
    order = Order.objects.select_related('package').filter(pk=intent.order_id).first()
    if not order or order.order_type != Order.ORDER_TYPE_NORMAL:
        return {'state': 'closed'}
    designation = None
    if intent.scope == 'dm':
        if not KookBinding.objects.filter(pk=intent.binding_id, player=player, bot_key=bot_key(),
                active=True, notifications_enabled=True, version=intent.binding_version).exists():
            return forbidden
        designation = OrderDesignation.objects.filter(pk=intent.designation_id, order=order, player=player).first()
        if not designation:
            return forbidden
    elif intent.scope != 'channel' or order.fulfillment_mode != 'public':
        return forbidden
    if escort_order_block_reason(order, player):
        return forbidden
    if order.status in {Order.STATUS_CANCELLED, Order.STATUS_COMPLETED}:
        return {'state': 'closed', 'order_no': order.order_no}
    if order.fulfillment_mode == 'targeted' and not order.paid:
        return {'state': 'closed'}
    if order.order_players.filter(player=player).exists():
        return {'state': 'joined', 'order_no': order.order_no}
    if designation:
        if not player.can_be_designated:
            return forbidden
        if designation.status != 'pending' or designation.expires_at <= timezone.now():
            return {'state': 'closed', 'order_no': order.order_no}
        if order.status not in {Order.STATUS_WAITING, Order.STATUS_IN_PROGRESS, Order.STATUS_READY_TO_START}:
            return {'state': 'closed', 'order_no': order.order_no}
        return {'state': 'invited', 'order_no': order.order_no}
    if not public_open(order):
        return {'state': 'closed', 'order_no': order.order_no}
    state = open_public_replacement(order)
    allowed = can_player_take_replacement(order, player, state) if state else can_player_grab_order_readonly(order, player)
    allowed = can_player_grab_after_room_timeout(lambda _o, _p: allowed, order, player)
    allowed = can_player_grab_with_discipline(lambda _o, _p: allowed, order, player)
    return {'state': 'available', 'order_no': order.order_no} if allowed else forbidden


def revalidate_order(envelope, delivery):
    """Worker callback: re-read facts, recipient and permissions, never writes."""
    from apps.kook_integration.models import KookOutboxEvent, KookBinding
    from .models import OrderDesignation
    from apps.players.escort_qualification import escort_order_block_reason
    scope = getattr(settings, 'KOOK_ORDER_NOTIFICATION_SCOPE', 'all')
    if scope not in {'all', 'designated_dm'}:
        return False
    if scope == 'designated_dm' and delivery.scope != 'dm':
        return False
    if delivery.scope == 'dm' or envelope.get('audience') == 'designated_dm':
        if delivery.scope != 'dm' or envelope.get('audience') != 'designated_dm':
            return False
        scoped_row = OrderDesignation.objects.filter(pk=envelope.get('designation_id'),
            order_id=envelope.get('order_id')).first()
        scoped_binding = scoped_dm_binding(scoped_row) if scoped_row else None
        if not scoped_binding or scoped_binding.pk != delivery.binding_id:
            return False
    order = Order.objects.select_related('package').filter(pk=envelope.get('order_id')).first()
    if not order:
        return False
    # A public terminal event closes one known remote stream, not the current
    # invitation to join. Later payment/status changes cannot undo that duty.
    if (envelope.get('audience') == 'public_channel' and
            envelope.get('event_type') in {'public_slots.closed', 'order.cancelled', 'replacement.resolved'}):
        from apps.kook_integration.models import KookMessageProjection, KookOrderProjection
        from apps.kook_integration.secrets import bot_key
        stream = KookMessageProjection.objects.filter(pk=delivery.stream_id,
            bot_key=bot_key(), scope='channel', target_id=delivery.target_id,
            order_id=order.pk, designation_key='', public_epoch=envelope.get('public_epoch', 0)).first()
        if (delivery.scope != 'channel' or delivery.operation != 'update' or not stream or
                not stream.remote_message_id or delivery.target_id != getattr(settings, 'KOOK_VERIFIED_CHANNEL_ID', '')):
            return False
        latest_close = KookOutboxEvent.objects.filter(kookdelivery__stream=stream).order_by('-revision').first()
        projection = KookOrderProjection.objects.filter(order=order).first()
        return bool(latest_close and latest_close.pk == delivery.event_id and
                    latest_close.payload == envelope and projection and
                    (projection.public_epoch > stream.public_epoch or
                     (projection.public_epoch == stream.public_epoch and not public_open(order))))
    latest = KookOutboxEvent.objects.filter(order_id=order.pk, designation_id=envelope.get('designation_id'),
            payload__audience=envelope.get('audience')).order_by('-revision').first()
    if not latest or envelope.get('revision') != latest.revision:
        return False
    if order.fulfillment_mode != envelope.get('fulfillment_mode') or order.paid != envelope.get('paid'):
        return False
    if envelope.get('audience') == 'public_channel':
        if not public_order_in_scope(order):
            return False
        if delivery.scope != 'channel' or order.fulfillment_mode != 'public':
            return False
        if envelope.get('safe_snapshot') != public_snapshot(order):
            return False
        closing = envelope.get('event_type') in {'public_slots.closed', 'order.cancelled', 'replacement.resolved'}
        return (not public_open(order)) if closing else public_open(order)
    if envelope.get('audience') != 'designated_dm' or delivery.scope != 'dm':
        return False
    binding = KookBinding.objects.filter(pk=delivery.binding_id, active=True,
                notifications_enabled=True, version=delivery.binding_version,
                kook_user_id=delivery.target_id).first()
    if not binding:
        return False
    player = eligible_player(binding.player_id)
    row = OrderDesignation.objects.filter(pk=envelope.get('designation_id'), order=order, player_id=binding.player_id).first()
    if not row or not player or not player.can_be_designated or escort_order_block_reason(order, player):
        return False
    if order.fulfillment_mode == 'targeted' and not order.paid:
        return False
    active = row.status == 'pending' and row.expires_at > timezone.now() and order.status in {Order.STATUS_WAITING, Order.STATUS_IN_PROGRESS, Order.STATUS_READY_TO_START}
    if envelope.get('event_type') == 'designation.closed':
        return not active
    return bool(active and envelope.get('safe_snapshot', {}).get('deadline_label') == row.expires_at.isoformat())
