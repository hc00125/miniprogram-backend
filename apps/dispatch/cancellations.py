"""Cancel unstarted staff-offline service; preserve all financial evidence."""
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError
from apps.common.money import money
from apps.orders.models import Order, OrderPlayer, OrderStatusLog
from apps.orders.cancel_signals import ORDER_PLAYER_CANCELLED_STATUS
from apps.orders.cancellation_models import OrderReplacementState
from apps.orders.kook_notifications import order_event_boundary
from apps.players.models import Player
from .models import DispatchReceipt
from .services import check_operator, conflict


OFFLINE_REFUND_NOTICE = '仅取消派单，不退钻石；线下退款未确认，请原客服单独核实处理。'


def cancellation_block_reason(order):
    if order.source != Order.SOURCE_STAFF:
        return '只有客服代派订单可在此取消，老板自助订单请走原取消/退款流程。'
    if order.status == Order.STATUS_CANCELLED:
        return '订单已取消。' + OFFLINE_REFUND_NOTICE
    if (order.status not in {Order.STATUS_WAITING, Order.STATUS_READY_TO_START}
            or order.start_time or order.timer_started_at or order.end_time
            or order.duration_minutes or order.paused_duration or order.is_paused
            or order.order_players.filter(status='已完成').exists()):
        return '仅支持未开打的待接单/待开打订单；已开打或已完成请联系管理员核实服务与结算。'
    if (order.order_type != Order.ORDER_TYPE_NORMAL or order.parent_order_id
            or order.fulfillment_mode != Order.FULFILLMENT_MODE_PUBLIC
            or order.renewal_orders.exists()):
        return '该订单存在特殊履约或续单记录，请联系管理员核实后处理。'
    receipt = DispatchReceipt.objects.filter(order=order).first()
    if (not order.paid or order.payment_method != 'staff_offline' or not receipt
            or money(receipt.received_amount) != money(order.total_amount)):
        return '线下收款记录不完整或不一致，请联系管理员核实，不能直接取消。'
    if (order.surcharge_guarded or order.surcharges.exists() or order.payments.exists()
            or order.player_earnings.exists()):
        return '订单存在支付、加价或工资记录，请联系管理员核实，不能直接取消。'
    return ''


@transaction.atomic
@order_event_boundary
def cancel_dispatch(order_no, data, actor):
    check_operator(actor)
    # Same parent row lock as grab/start/complete; revalidate after locking.
    order = Order.objects.select_for_update().filter(order_no=order_no).first()
    if order is None:
        raise NotFound('订单不存在')
    if order.source != Order.SOURCE_STAFF:
        conflict(cancellation_block_reason(order))
    reason = data.get('reason')
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 200:
        raise ValidationError({'reason': '请填写1至200字的取消原因'})
    # A retry is a read of the original terminal result, not a new cancellation.
    if order.status == Order.STATUS_CANCELLED:
        return order
    blocked = cancellation_block_reason(order)
    if blocked:
        conflict(blocked)
    now = timezone.now()
    previous_status = order.status
    order.status = Order.STATUS_CANCELLED
    order.cancel_reason = reason.strip()
    order.canceled_at = now
    order.save(update_fields=['status', 'cancel_reason', 'canceled_at'])

    # Retain lineup and entry history; stop deadlines, without a player penalty.
    relations = list(OrderPlayer.objects.select_for_update().filter(order=order)
                     .exclude(status=ORDER_PLAYER_CANCELLED_STATUS).order_by('pk'))
    if relations:
        OrderPlayer.objects.filter(pk__in=[row.pk for row in relations]).update(
            status=ORDER_PLAYER_CANCELLED_STATUS, room_join_deadline=None)
        for player in Player.objects.select_for_update().filter(
                pk__in={row.player_id for row in relations}).order_by('pk'):
            player.total_orders = max(0, int(player.total_orders or 0) - 1)
            player.save(update_fields=['total_orders'])
    order.designations.filter(status__in=['pending', 'accepted']).update(status='cancelled', responded_at=now)
    OrderReplacementState.objects.filter(order=order).exclude(status=OrderReplacementState.STATUS_RESOLVED).update(
        status=OrderReplacementState.STATUS_RESOLVED, missing_slots=0, current_designation=None,
        resolved_at=now, updated_at=now)
    OrderStatusLog.objects.create(order=order, from_status=previous_status, to_status=order.status,
        operator=actor, reason='客服取消派单：' + reason.strip() + '；' + OFFLINE_REFUND_NOTICE)
    # paid/payment_method/receipt/amount/wallets/earnings/refunds are not changed.
    return order
