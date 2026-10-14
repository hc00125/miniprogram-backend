from datetime import timedelta

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .matching_models import OrderMatchingWindow
from .models import Order, OrderStatusLog


MATCHING_REMINDER_MINUTES = 10
MATCHING_NO_FAULT_EXIT_MINUTES = 15
MATCHING_DECISION_MINUTES = 30
MATCHING_EXTENSION_MINUTES = 30
MATCHING_HARD_TIMEOUT_MINUTES = 120
PUBLIC_MATCHING_TIMEOUT_REASON = '公开匹配超过120分钟仍未凑齐，系统自动取消'


def is_public_unpaid_matching(order):
    return bool(
        order
        and order.order_type == Order.ORDER_TYPE_NORMAL
        and order.fulfillment_mode == Order.FULFILLMENT_MODE_PUBLIC
        and not order.paid
        and order.status in {Order.STATUS_WAITING, Order.STATUS_PENDING_PAYMENT}
    )


def ensure_matching_window(order, now=None):
    if not is_public_unpaid_matching(order):
        return None
    now = now or timezone.now()
    started_at = order.created_at or now
    window, _ = OrderMatchingWindow.objects.get_or_create(
        order=order,
        defaults={
            'started_at': started_at,
            'deadline_at': started_at + timedelta(minutes=MATCHING_DECISION_MINUTES),
        },
    )
    return window


def matching_hard_deadline(window):
    return window.started_at + timedelta(minutes=MATCHING_HARD_TIMEOUT_MINUTES)


def lineup_is_full(order):
    return order.order_players.count() >= int(order.required_players or 0)


def matching_visibility(order):
    """Explain whether a public order is actually visible to eligible online players."""
    from apps.catalog.models import PlayerType
    from apps.players.escort_qualification import (
        escort_order_block_reason,
        order_requires_escort_qualification,
    )
    from apps.players.models import Player

    from .designations import pending_designation_count
    from .discipline import discipline_block_reason
    from .services import can_player_grab_order, remaining_type_slots

    current_players = order.order_players.count()
    pending_designations = pending_designation_count(order)
    type_slots = remaining_type_slots(order)
    required_types = []
    for item in type_slots:
        player_type = PlayerType.objects.filter(id=item.get('type_id')).first()
        if not player_type:
            continue
        required_types.append({
            'id': player_type.id,
            'name': player_type.name,
            'priority': int(player_type.priority or 0),
            'count': int(item.get('count') or 0),
        })
    required_types.sort(key=lambda item: item['priority'])
    type_slot_count = sum(item['count'] for item in required_types)
    public_slots = max(
        0,
        int(order.required_players or 0) - current_players - pending_designations - type_slot_count,
    )

    online_players = list(
        Player.objects.filter(
            is_online=True,
            status=Player.STATUS_APPROVED,
        ).select_related('player_type')
    )
    eligible_count = 0
    for player in online_players:
        if not player.can_accept_orders or discipline_block_reason(player):
            continue
        if escort_order_block_reason(order, player):
            continue
        if can_player_grab_order(order, player):
            eligible_count += 1

    if eligible_count:
        visibility_status = 'visible'
        visibility_message = f'订单已发布，当前有 {eligible_count} 位在线陪玩符合条件并可看到'
    elif not online_players:
        visibility_status = 'no_online_players'
        visibility_message = '订单已发布，但当前没有在线陪玩'
    else:
        visibility_status = 'no_eligible_players'
        visibility_message = '订单已发布，但当前在线陪玩均不符合剩余名额要求或已在本单阵容中'

    return {
        'public_slots': public_slots,
        'pending_designation_slots': pending_designations,
        'typed_slots': type_slot_count,
        'required_player_types': required_types,
        'requires_escort_qualification': order_requires_escort_qualification(order),
        'online_player_count': len(online_players),
        'eligible_online_player_count': eligible_count,
        'visibility_status': visibility_status,
        'visibility_message': visibility_message,
    }


def matching_payload(order, now=None):
    if not is_public_unpaid_matching(order):
        return {'active': False}
    now = now or timezone.now()
    window = ensure_matching_window(order, now)
    elapsed_seconds = max(0, int((now - window.started_at).total_seconds()))
    remaining_seconds = max(0, int((window.deadline_at - now).total_seconds()))
    hard_deadline_at = matching_hard_deadline(window)
    hard_remaining_seconds = max(0, int((hard_deadline_at - now).total_seconds()))
    current_players = order.order_players.count()
    missing_slots = max(0, int(order.required_players or 0) - current_players)
    return {
        'active': True,
        'started_at': window.started_at,
        'deadline_at': window.deadline_at,
        'elapsed_seconds': elapsed_seconds,
        'remaining_seconds': remaining_seconds,
        'reminder_due': elapsed_seconds >= MATCHING_REMINDER_MINUTES * 60,
        'players_can_exit_without_penalty': elapsed_seconds >= MATCHING_NO_FAULT_EXIT_MINUTES * 60,
        'decision_required': remaining_seconds <= 0,
        'extension_count': window.extension_count,
        'current_players': current_players,
        'required_players': order.required_players,
        'missing_slots': missing_slots,
        'hard_timeout_minutes': MATCHING_HARD_TIMEOUT_MINUTES,
        'hard_deadline_at': hard_deadline_at,
        'hard_remaining_seconds': hard_remaining_seconds,
        'auto_cancel_due': missing_slots > 0 and hard_remaining_seconds <= 0,
        **matching_visibility(order),
    }


@transaction.atomic
def extend_matching(order, operator=None, now=None):
    now = now or timezone.now()
    order = Order.objects.select_for_update(of=('self',)).get(pk=order.pk)
    if not is_public_unpaid_matching(order):
        raise ValidationError({'detail': '当前订单不在公开匹配阶段，不能继续等待'})
    window = ensure_matching_window(order, now)
    window = OrderMatchingWindow.objects.select_for_update(of=('self',)).get(pk=window.pk)
    hard_deadline_at = matching_hard_deadline(window)
    if now >= hard_deadline_at:
        raise ValidationError({'detail': '公开匹配已达到2小时上限，不能继续等待'})

    requested_deadline = max(window.deadline_at, now) + timedelta(minutes=MATCHING_EXTENSION_MINUTES)
    window.deadline_at = min(requested_deadline, hard_deadline_at)
    window.extension_count += 1
    window.last_extended_at = now
    window.save(update_fields=['deadline_at', 'extension_count', 'last_extended_at', 'updated_at'])
    OrderStatusLog.objects.create(
        order=order,
        from_status=order.status,
        to_status=order.status,
        operator=operator,
        reason='老板选择继续等待匹配；公开匹配总时长最多2小时',
    )
    return window


@transaction.atomic
def expire_public_matching_order(order_or_id, now=None):
    """Cancel one unpaid public order after its two-hour hard matching limit.

    Only incomplete public orders still in ``待接单`` are eligible.  Targeted
    orders and full lineups waiting for payment are deliberately excluded.
    Existing unpaid-order cancellation signals release joined players without
    penalties while preserving the relations for audit.
    """
    now = now or timezone.now()
    order_id = getattr(order_or_id, 'pk', order_or_id)
    order = (
        Order.objects
        .select_for_update(of=('self',))
        .filter(pk=order_id)
        .first()
    )
    if not order:
        return False
    if (
        order.order_type != Order.ORDER_TYPE_NORMAL
        or order.fulfillment_mode != Order.FULFILLMENT_MODE_PUBLIC
        or order.paid
        or order.status != Order.STATUS_WAITING
    ):
        return False

    window = ensure_matching_window(order, now)
    if not window:
        return False
    window = OrderMatchingWindow.objects.select_for_update(of=('self',)).get(pk=window.pk)
    if now < matching_hard_deadline(window):
        return False
    if lineup_is_full(order):
        return False

    from .services import cancel_order

    cancel_order(order, reason=PUBLIC_MATCHING_TIMEOUT_REASON, operator=None)
    return True


def expire_public_matching_orders(now=None, limit=500):
    """Expire due public matching orders in bounded batches.

    This is intentionally separate from designated-invitation expiry.  Run it
    from a server-side scheduler (for example once per minute) so cancellation
    does not depend on either the boss or a player keeping a page open.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=MATCHING_HARD_TIMEOUT_MINUTES)
    candidate_ids = list(
        OrderMatchingWindow.objects
        .filter(
            started_at__lte=cutoff,
            order__order_type=Order.ORDER_TYPE_NORMAL,
            order__fulfillment_mode=Order.FULFILLMENT_MODE_PUBLIC,
            order__paid=False,
            order__status=Order.STATUS_WAITING,
        )
        .order_by('started_at')
        .values_list('order_id', flat=True)[:max(1, int(limit or 500))]
    )
    expired = 0
    for order_id in candidate_ids:
        if expire_public_matching_order(order_id, now=now):
            expired += 1
    return expired


def relation_can_exit_without_penalty(order, relation, now=None):
    now = now or timezone.now()
    if not is_public_unpaid_matching(order) or not relation or not relation.grab_time:
        return False
    return relation.grab_time <= now - timedelta(minutes=MATCHING_NO_FAULT_EXIT_MINUTES)


@receiver(post_save, sender=Order, dispatch_uid='ensure_public_order_matching_window')
def ensure_public_order_matching_window(sender, instance, created, **kwargs):
    if created and is_public_unpaid_matching(instance):
        ensure_matching_window(instance)
