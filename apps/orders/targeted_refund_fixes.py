from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.payments.models import Payment
from apps.payments.refund_integrity import ensure_payment_refund

from .models import Order, OrderStatusLog


@transaction.atomic
def cancel_targeted_order(order, reason, operator=None):
    """Cancel a targeted paid order only after its refund is safely created.

    Balance refunds are settled into the wallet inside the same outer database
    transaction.  If payment/refund data is inconsistent or wallet settlement
    fails, the cancellation is rolled back instead of leaving an already
    cancelled order with missing customer funds.
    """
    locked = (
        Order.objects
        .select_for_update(of=('self',))
        .get(pk=order.pk)
    )
    if locked.fulfillment_mode != Order.FULFILLMENT_MODE_TARGETED:
        return None

    # Idempotent retry: an already-cancelled order may have been created by an
    # older buggy deployment.  Do not invent another refund here; historical
    # repair stays explicit through reconcile_balance_refunds.
    if locked.status == Order.STATUS_CANCELLED:
        order.status = locked.status
        order.canceled_at = locked.canceled_at
        order.cancel_reason = locked.cancel_reason
        return locked.refunds.order_by('-created_at').first()

    previous_status = locked.status
    refund = None
    if locked.paid:
        payment = (
            Payment.objects
            .filter(order=locked, status__in=['paid', 'refunded'])
            .order_by('-paid_at', '-created_at')
            .first()
        )
        if not payment:
            raise ValidationError({
                'detail': '订单已标记支付成功，但找不到对应已支付记录；已阻止自动取消，请人工核对账务'
            })
        refund = ensure_payment_refund(
            payment.payment_no,
            payment.amount,
            reason=reason,
            operator=operator,
        )
        if not refund or refund.status != 'succeeded':
            raise ValidationError({'detail': '退款未成功入账，已阻止订单取消'})

    locked.status = Order.STATUS_CANCELLED
    locked.canceled_at = timezone.now()
    locked.cancel_reason = reason
    locked.save(update_fields=['status', 'canceled_at', 'cancel_reason'])

    refund_note = ''
    if refund:
        refund_note = '，余额已退回钱包' if refund.status == 'succeeded' else '，已创建退款申请'
    OrderStatusLog.objects.create(
        order=locked,
        from_status=previous_status,
        to_status=locked.status,
        operator=operator,
        reason=f'{reason}{refund_note}',
    )

    # Keep callers holding the original instance in sync with the locked copy.
    order.status = locked.status
    order.canceled_at = locked.canceled_at
    order.cancel_reason = locked.cancel_reason
    return refund
