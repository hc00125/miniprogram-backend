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
    current_players = order.order_players.count()
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
        'missing_slots': max(0, int(order.required_players or 0) - current_players),
        **matching_visibility(order),
    }


@transaction.atomic
def extend_matching(order, operator=None, now=None):
    now = now or timezone.now()
    order = Order.objects.select_for_update().get(pk=order.pk)
    if not is_public_unpaid_matching(order):
        raise ValidationError({'detail': '当前订单不在公开匹配阶段，不能继续等待'})
    window = ensure_matching_window(order, now)
    window = OrderMatchingWindow.objects.select_for_update().get(pk=window.pk)
    window.deadline_at = max(window.deadline_at, now) + timedelta(minutes=MATCHING_EXTENSION_MINUTES)
    window.extension_count += 1
    window.last_extended_at = now
    window.save(update_fields=['deadline_at', 'extension_count', 'last_extended_at', 'updated_at'])
    OrderStatusLog.objects.create(
        order=order,
        from_status=order.status,
        to_status=order.status,
        operator=operator,
        reason=f'老板选择继续等待匹配，延长 {MATCHING_EXTENSION_MINUTES} 分钟',
    )
    return window


def relation_can_exit_without_penalty(order, relation, now=None):
    now = now or timezone.now()
    if not is_public_unpaid_matching(order) or not relation or not relation.grab_time:
        return False
    return relation.grab_time <= now - timedelta(minutes=MATCHING_NO_FAULT_EXIT_MINUTES)


@receiver(post_save, sender=Order, dispatch_uid='ensure_public_order_matching_window')
def ensure_public_order_matching_window(sender, instance, created, **kwargs):
    if created and is_public_unpaid_matching(instance):
        ensure_matching_window(instance)
