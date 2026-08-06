from datetime import timedelta

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.payments.models import Payment
from apps.players.models import Player

from .cancellation_models import OrderReplacementState
from .models import Order, OrderPlayer, OrderStatusLog


ROOM_ENTRY_TTL_MINUTES = 10
ROOM_ENTRY_TIMEOUT_REASON_PREFIX = '陪玩进入房间超时'


def start_room_entry_window(order, started_at=None, payment_verified=False):
    """Start the room-entry clock only after the boss payment is verified."""
    if not order or not order.pk or (not order.paid and not payment_verified):
        return 0
    started_at = started_at or order.payment_confirmed_at or timezone.now()
    deadline = started_at + timedelta(minutes=ROOM_ENTRY_TTL_MINUTES)
    return OrderPlayer.objects.filter(order=order, status='已接单').update(
        room_join_deadline=deadline,
        room_join_confirmed_at=None,
        room_join_status=OrderPlayer.ROOM_ENTRY_PENDING,
    )


def can_player_grab_after_room_timeout(original_can_grab, order, player):
    """A player treated as rejecting this order cannot immediately reclaim it."""
    user_id = getattr(player, 'user_id', None)
    if user_id and OrderStatusLog.objects.filter(
        order=order,
        operator_id=user_id,
        reason__startswith=ROOM_ENTRY_TIMEOUT_REASON_PREFIX,
    ).exists():
        return False
    return original_can_grab(order, player)


def finalize_lineup_with_paid_replacement(original_finalize, order, operator=None, reason='接单人数已满，等待老板付款'):
    """Complete a paid replacement lineup without opening a second payment window."""
    if (
        order.order_players.count() >= order.required_players
        and order.status == Order.STATUS_WAITING
        and order.order_type == Order.ORDER_TYPE_NORMAL
        and order.fulfillment_mode == Order.FULFILLMENT_MODE_PUBLIC
        and order.paid
    ):
        old_status = order.status
        order.status = Order.STATUS_READY_TO_START
        order.save(update_fields=['status'])
        OrderStatusLog.objects.create(
            order=order,
            from_status=old_status,
            to_status=order.status,
            operator=operator,
            reason='已付款补位单人数补齐，无需老板重复支付',
        )
        return order
    return original_finalize(order, operator, reason)


@receiver(post_save, sender=OrderPlayer, dispatch_uid='normalize_room_entry_deadline')
def normalize_room_entry_deadline(sender, instance, created, **kwargs):
    if not created:
        return
    order = instance.order
    if order.paid:
        deadline = timezone.now() + timedelta(minutes=ROOM_ENTRY_TTL_MINUTES)
        OrderPlayer.objects.filter(pk=instance.pk).update(
            room_join_deadline=deadline,
            room_join_confirmed_at=None,
            room_join_status=OrderPlayer.ROOM_ENTRY_PENDING,
        )
    elif instance.room_join_deadline is not None:
        OrderPlayer.objects.filter(pk=instance.pk).update(room_join_deadline=None)


@receiver(post_save, sender=Payment, dispatch_uid='start_room_entry_after_payment')
def start_room_entry_after_payment(sender, instance, **kwargs):
    if instance.status != 'paid':
        return
    start_room_entry_window(
        instance.order,
        instance.paid_at or timezone.now(),
        payment_verified=True,
    )


@receiver(post_save, sender=Order, dispatch_uid='keep_paid_replacement_order_ready')
def keep_paid_replacement_order_ready(sender, instance, **kwargs):
    if not (
        instance.order_type == Order.ORDER_TYPE_NORMAL
        and instance.fulfillment_mode == Order.FULFILLMENT_MODE_PUBLIC
        and instance.paid
    ):
        return

    should_restore = instance.status == Order.STATUS_PENDING_PAYMENT
    if instance.status == Order.STATUS_WAITING:
        should_restore = OrderReplacementState.objects.filter(
            order_id=instance.pk,
            mode=OrderReplacementState.MODE_PUBLIC,
            status=OrderReplacementState.STATUS_OPEN,
        ).exists()
    if not should_restore:
        return

    updated = Order.objects.filter(
        pk=instance.pk,
        paid=True,
        status=instance.status,
    ).update(status=Order.STATUS_READY_TO_START)
    if updated:
        OrderStatusLog.objects.create(
            order=instance,
            from_status=instance.status,
            to_status=Order.STATUS_READY_TO_START,
            reason='已付款订单进入紧急补位，保留待开打状态且无需重复支付',
        )


def _is_requeueable(order):
    return bool(
        order.order_type == Order.ORDER_TYPE_NORMAL
        and order.fulfillment_mode == Order.FULFILLMENT_MODE_PUBLIC
        and order.paid
        and not order.timer_started_at
        and order.status in {Order.STATUS_WAITING, Order.STATUS_READY_TO_START}
    )


def _open_room_timeout_replacement(order, relation, player_name, now):
    remaining_count = order.order_players.exclude(pk=relation.pk).count()
    missing_slots = max(1, int(order.required_players or 0) - remaining_count)
    player_type = getattr(relation.player, 'player_type', None)
    state, _ = OrderReplacementState.objects.select_for_update().get_or_create(
        order=order,
        defaults={
            'mode': OrderReplacementState.MODE_PUBLIC,
            'status': OrderReplacementState.STATUS_OPEN,
            'missing_slots': missing_slots,
            'resume_status': Order.STATUS_READY_TO_START,
        },
    )
    state.mode = OrderReplacementState.MODE_PUBLIC
    state.status = OrderReplacementState.STATUS_OPEN
    state.missing_slots = missing_slots
    state.resume_status = Order.STATUS_READY_TO_START
    state.remaining_minutes = max(1, int(float(order.booked_hours or 1) * 60))
    state.cancelled_player_name = player_name
    state.required_player_type_id = getattr(player_type, 'id', None)
    state.required_player_type_name = getattr(player_type, 'name', '') or ''
    state.latest_cancellation = None
    state.current_designation = None
    state.resolved_at = None
    state.save()
    return state


@transaction.atomic
def expire_room_entry_relation(relation_id, now=None):
    """Treat a missed room-entry deadline as rejection and reopen one public slot."""
    now = now or timezone.now()
    relation = (
        OrderPlayer.objects
        .select_for_update(of=('self',))
        .select_related('order', 'player__user', 'player__player_type')
        .filter(pk=relation_id)
        .first()
    )
    if not relation:
        return False
    order = Order.objects.select_for_update().get(pk=relation.order_id)
    if not _is_requeueable(order):
        return False
    if relation.room_join_status not in {OrderPlayer.ROOM_ENTRY_PENDING, OrderPlayer.ROOM_ENTRY_OVERDUE}:
        return False
    if not relation.room_join_deadline or relation.room_join_deadline > now:
        return False

    player = Player.objects.select_for_update().get(pk=relation.player_id)
    old_status = order.status
    player_name = player.name
    operator = player.user if getattr(player, 'user_id', None) else None

    _open_room_timeout_replacement(order, relation, player_name, now)
    relation.delete()
    player.total_orders = max(0, int(player.total_orders or 0) - 1)
    player.save(update_fields=['total_orders'])

    order.status = Order.STATUS_READY_TO_START
    order.save(update_fields=['status'])
    OrderStatusLog.objects.create(
        order=order,
        from_status=old_status,
        to_status=Order.STATUS_READY_TO_START,
        operator=operator,
        reason=f'{ROOM_ENTRY_TIMEOUT_REASON_PREFIX}：{player_name} 未在10分钟内确认进入，视为拒单；已付款订单保持待开打并进入紧急补位',
    )
    return True


def expire_due_room_entries(now=None):
    now = now or timezone.now()
    relation_ids = list(
        OrderPlayer.objects.filter(
            room_join_status__in=[OrderPlayer.ROOM_ENTRY_PENDING, OrderPlayer.ROOM_ENTRY_OVERDUE],
            room_join_deadline__isnull=False,
            room_join_deadline__lte=now,
            order__paid=True,
            order__timer_started_at__isnull=True,
            order__order_type=Order.ORDER_TYPE_NORMAL,
            order__fulfillment_mode=Order.FULFILLMENT_MODE_PUBLIC,
            order__status__in=[Order.STATUS_WAITING, Order.STATUS_READY_TO_START],
        ).values_list('id', flat=True)
    )
    return sum(1 for relation_id in relation_ids if expire_room_entry_relation(relation_id, now=now))
