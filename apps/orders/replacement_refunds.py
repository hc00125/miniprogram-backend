from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.common.money import money
from apps.payments.models import Payment
from apps.payments.refund_integrity import ensure_payment_refund

from .cancellation_models import OrderReplacementState
from .models import Order, OrderDesignation, OrderPlayer, OrderStatusLog


MINUTE = Decimal('60')
ORDER_PLAYER_CANCELLED_STATUS = '订单已取消'


def _root_order(order):
    if order.order_type == Order.ORDER_TYPE_RENEWAL and order.parent_order_id:
        return order.parent_order
    return order


def _service_orders(root):
    """Return the paid service-time group in chronological order.

    A renewal order is marked completed as soon as its paid duration is appended to
    the root service, so completed+paid renewals are the relevant paid duration
    segments for a remaining-service refund.
    """
    renewals = list(
        root.renewal_orders
        .filter(
            order_type=Order.ORDER_TYPE_RENEWAL,
            paid=True,
            status=Order.STATUS_COMPLETED,
        )
        .order_by('renewal_index', 'id')
    )
    return [root, *renewals]


def _segment_minutes(service_order):
    hours = Decimal(str(service_order.booked_hours or 0))
    minutes = int((hours * MINUTE).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    return max(1, minutes)


def _paid_payment(service_order):
    payment = (
        Payment.objects
        .select_for_update(of=('self',))
        .filter(order=service_order, status__in=['paid', 'refunded'])
        .order_by('-paid_at', '-created_at', '-id')
        .first()
    )
    if not payment:
        raise ValidationError({
            'detail': (
                f'订单 {service_order.order_no} 已标记支付成功，但找不到可退款的支付记录；'
                '已阻止取消，请先核对支付数据'
            )
        })
    return payment


def _remaining_minutes_now(root, total_minutes, now):
    if not root.timer_started_at:
        return total_minutes
    elapsed_seconds = max(
        0,
        int((now - root.timer_started_at).total_seconds()) - int(root.paused_duration or 0),
    )
    remaining_seconds = max(0, total_minutes * 60 - elapsed_seconds)
    if remaining_seconds <= 0:
        return 0
    return (remaining_seconds + 59) // 60


def _refund_plan(root, state, now):
    """Split the unfulfilled duration across original/renewal payment records.

    Remaining service is consumed from the tail of the order group: later renewal
    duration is refunded first, then the unconsumed tail of the original order.
    This keeps each refund tied to the payment that actually funded that duration.
    """
    segments = []
    total_minutes = 0
    for service_order in _service_orders(root):
        if not service_order.paid:
            continue
        minutes = _segment_minutes(service_order)
        payment = _paid_payment(service_order)
        segments.append((service_order, payment, minutes))
        total_minutes += minutes

    if not segments:
        raise ValidationError({'detail': '当前订单没有可退款的已支付服务记录'})

    remaining_minutes = state.remaining_minutes
    if remaining_minutes is None:
        remaining_minutes = _remaining_minutes_now(root, total_minutes, now)
    remaining_minutes = max(0, min(int(remaining_minutes or 0), total_minutes))
    if remaining_minutes <= 0:
        return [], 0, total_minutes

    plan = []
    minutes_left = remaining_minutes
    for service_order, payment, minutes in reversed(segments):
        if minutes_left <= 0:
            break
        refundable_minutes = min(minutes_left, minutes)
        if refundable_minutes >= minutes:
            target_amount = money(payment.amount)
        else:
            target_amount = money(
                Decimal(payment.amount) * Decimal(refundable_minutes) / Decimal(minutes)
            )
        if target_amount > 0:
            plan.append({
                'order': service_order,
                'payment': payment,
                'minutes': refundable_minutes,
                'target_amount': target_amount,
            })
        minutes_left -= refundable_minutes

    return list(reversed(plan)), remaining_minutes, total_minutes


def _cancel_pending_renewals(root, now, operator=None):
    pending = list(
        root.renewal_orders
        .select_for_update()
        .filter(paid=False, status=Order.STATUS_PENDING_PAYMENT)
        .order_by('renewal_index', 'id')
    )
    if not pending:
        return

    from apps.payments.services import close_unpaid_payments_for_order

    for renewal in pending:
        previous_status = renewal.status
        close_unpaid_payments_for_order(renewal, reason='原订单取消剩余服务，续单同步取消')
        renewal.status = Order.STATUS_CANCELLED
        renewal.canceled_at = now
        renewal.cancel_reason = '原订单取消剩余服务'
        renewal.save(update_fields=['status', 'canceled_at', 'cancel_reason'])
        OrderStatusLog.objects.create(
            order=renewal,
            from_status=previous_status,
            to_status=renewal.status,
            operator=operator,
            reason='原订单取消剩余服务，未支付续单同步取消',
        )


def _release_remaining_players(root):
    OrderPlayer.objects.filter(order=root).exclude(status=ORDER_PLAYER_CANCELLED_STATUS).update(
        status=ORDER_PLAYER_CANCELLED_STATUS,
        room_join_deadline=None,
    )


def _cancel_designations(root, now):
    OrderDesignation.objects.filter(
        order=root,
        status__in=[OrderDesignation.STATUS_PENDING, OrderDesignation.STATUS_ACCEPTED],
    ).update(
        status=OrderDesignation.STATUS_CANCELLED,
        responded_at=now,
    )


@transaction.atomic
def cancel_remaining_and_refund(order, operator=None):
    """Cancel the active replacement flow and refund unfulfilled service to wallet.

    This replaces the old ``cancel_requested -> customer service calculation`` flow.
    The refund amount is derived from the captured remaining service duration, split
    across the root order and paid renewals, and settled to the boss wallet before
    the order is marked cancelled.  Any accounting failure rolls the whole action
    back so a cancelled order can never be left without its corresponding refund.
    """
    root_id = order.parent_order_id or order.id
    root = (
        Order.objects
        .select_for_update(of=('self',))
        .select_related('boss_user')
        .get(pk=root_id)
    )
    state = (
        OrderReplacementState.objects
        .select_for_update()
        .filter(order=root)
        .first()
    )
    if not state or state.status not in {
        OrderReplacementState.STATUS_OPEN,
        OrderReplacementState.STATUS_CANCEL_REQUESTED,
    }:
        raise ValidationError({'detail': '当前没有可取消的剩余服务'})
    if not root.paid:
        raise ValidationError({'detail': '订单尚未支付，请直接取消订单'})
    if root.status == Order.STATUS_CANCELLED:
        raise ValidationError({'detail': '订单已经取消'})

    now = timezone.now()
    plan, remaining_minutes, _total_minutes = _refund_plan(root, state, now)

    refunded_amount = Decimal('0.00')
    refunded_order_nos = []
    reason = '指定陪玩退出，老板取消未履行的剩余服务'
    for item in plan:
        refund = ensure_payment_refund(
            item['payment'].payment_no,
            item['target_amount'],
            reason=reason,
            operator=operator,
        )
        if not refund or refund.status != 'succeeded':
            raise ValidationError({'detail': '退款未成功入账，已阻止订单取消'})
        refunded_amount += money(item['target_amount'])
        refunded_order_nos.append(item['order'].order_no)
        OrderStatusLog.objects.create(
            order=item['order'],
            from_status=item['order'].status,
            to_status=item['order'].status,
            operator=operator,
            reason=(
                f'取消剩余服务自动退款：未履行 {item["minutes"]} 分钟，'
                f'退款 ¥{money(item["target_amount"]):.2f} 已退回老板钱包'
            ),
        )

    _cancel_designations(root, now)
    _release_remaining_players(root)
    _cancel_pending_renewals(root, now, operator=operator)

    previous_status = root.status
    root.status = Order.STATUS_CANCELLED
    root.canceled_at = now
    root.cancel_reason = reason
    root.is_paused = False
    root.last_paused_at = None
    root.save(update_fields=[
        'status', 'canceled_at', 'cancel_reason', 'is_paused', 'last_paused_at',
    ])

    state.status = OrderReplacementState.STATUS_RESOLVED
    state.missing_slots = 0
    state.current_designation = None
    state.resolved_at = now
    state.save(update_fields=[
        'status', 'missing_slots', 'current_designation', 'resolved_at', 'updated_at',
    ])

    OrderStatusLog.objects.create(
        order=root,
        from_status=previous_status,
        to_status=root.status,
        operator=operator,
        reason=(
            f'老板取消未履行的剩余服务，共 {remaining_minutes} 分钟；'
            f'自动退款 ¥{money(refunded_amount):.2f} 已退回钱包，无需客服核算'
        ),
    )

    # Keep the instance held by the API view in sync for subsequent serialization.
    order.status = root.status
    order.canceled_at = root.canceled_at
    order.cancel_reason = root.cancel_reason

    return {
        'state': state,
        'refund_amount': money(refunded_amount),
        'remaining_minutes': remaining_minutes,
        'refunded_order_nos': refunded_order_nos,
    }
