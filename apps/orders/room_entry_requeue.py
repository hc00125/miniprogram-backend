from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.payments.models import Payment

from .cancellation_models import OrderReplacementState
from .models import Order, OrderPlayer, OrderStatusLog


# 进入老板房间改为主动确认，不再设置倒计时或超时处罚。
# 保留旧常量名称，避免历史代码/日志引用失效；0 表示当前规则无时限。
ROOM_ENTRY_TTL_MINUTES = 0
ROOM_ENTRY_TIMEOUT_REASON_PREFIX = '陪玩进入房间超时'


def start_room_entry_window(order, started_at=None, payment_verified=False):
    """老板支付确认后开放“进入房间”确认，但不设置截止时间。"""
    if not order or not order.pk or (not order.paid and not payment_verified):
        return 0
    return OrderPlayer.objects.filter(order=order, status='已接单').update(
        room_join_deadline=None,
        room_join_confirmed_at=None,
        room_join_status=OrderPlayer.ROOM_ENTRY_PENDING,
    )


def can_player_grab_after_room_timeout(original_can_grab, order, player):
    """只保留历史超时记录的兼容判断；新规则不会再产生超时记录。"""
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
        OrderPlayer.objects.filter(pk=instance.pk).update(
            room_join_deadline=None,
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


def expire_room_entry_relation(relation_id, now=None):
    """兼容旧调用：进入房间确认已无超时规则，因此永不自动释放陪玩。"""
    return False


def expire_due_room_entries(now=None):
    """兼容旧定时任务：当前规则不再按进入房间时间自动处理任何订单。"""
    return 0
