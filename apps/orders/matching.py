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
