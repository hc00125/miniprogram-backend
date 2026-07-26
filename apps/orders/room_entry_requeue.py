from datetime import timedelta

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.payments.models import Payment
from apps.players.models import Player

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
    if (
        instance.order_type == Order.ORDER_TYPE_NORMAL
        and instance.fulfillment_mode == Order.FULFILLMENT_MODE_PUBLIC
        and instance.paid
        and instance.status == Order.STATUS_PENDING_PAYMENT
    ):
        updated = Order.objects.filter(pk=instance.pk, paid=True, status=Order.STATUS_PENDING_PAYMENT).update(
            status=Order.STATUS_READY_TO_START
        )
        if updated:
            OrderStatusLog.objects.create(
                order=instance,
                from_status=Order.STATUS_PENDING_PAYMENT,
                to_status=Order.STATUS_READY_TO_START,
                reason='已付款补位单人数补齐，无需老板重复支付',
            )


def _is_requeueable(order):
    return bool(
        order.order_type == Order.ORDER_TYPE_NORMAL
        and order.fulfillment_mode == Order.FULFILLMENT_MODE_PUBLIC
        and order.paid
        and not order.timer_started_at
        and order.status in {Order.STATUS_WAITING, Order.STATUS_READY_TO_START}
    )


@transaction.atomic
def expire_room_entry_relation(relation_id, now=None):
    """Treat a missed room-entry deadline as rejection and reopen one public slot."""
    now = now or timezone.now()
    relation = (
        OrderPlayer.objects
        .select_for_update()
        .select_related('order', 'player__user')
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

    relation.delete()
    player.total_orders = max(0, int(player.total_orders or 0) - 1)
    player.save(update_fields=['total_orders'])

    order.status = Order.STATUS_WAITING
    order.save(update_fields=['status'])
    OrderStatusLog.objects.create(
        order=order,
        from_status=old_status,
        to_status=Order.STATUS_WAITING,
        operator=operator,
        reason=f'{ROOM_ENTRY_TIMEOUT_REASON_PREFIX}：{player_name} 未在10分钟内确认进入，视为拒单，名额已转入公共抢单大厅',
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
