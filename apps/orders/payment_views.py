import logging

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.payments.models import Payment
from apps.payments.virtualpay import VIRTUAL_CHANNEL, VirtualPaymentError, query_virtual_payment

from .boss_views import can_access_order, forbidden_response, get_order_or_response
from .models import Order
from .payment_deadlines import expire_due_unpaid_order, payment_deadline_payload
from .replacements import replacement_payload
from .serializers import BossOrderDetailSerializer


logger = logging.getLogger(__name__)


def reconcile_pending_virtual_payment(order, user):
    """主动核验该业务订单最近的虚拟支付单，修复微信已扣款但本地仍待支付。"""
    if order.paid or order.status != Order.STATUS_PENDING_PAYMENT:
        return False

    payments = list(
        Payment.objects
        .filter(order=order, channel=VIRTUAL_CHANNEL)
        .order_by('-created_at')[:5]
    )
    if not payments:
        return False

    for payment in payments:
        try:
            synced = query_virtual_payment(payment.payment_no, user)
        except VirtualPaymentError as exc:
            logger.warning(
                '[虚拟支付] 订单详情主动核验失败 order_no=%s payment_no=%s error=%s',
                order.order_no,
                payment.payment_no,
                exc,
            )
            return False
        except Exception:
            logger.exception(
                '[虚拟支付] 订单详情主动核验异常 order_no=%s payment_no=%s',
                order.order_no,
                payment.payment_no,
            )
            return False

        if synced.status == 'paid' or getattr(synced.order, 'paid', False):
            return True

    return False


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def order_detail(request, order_no):
    order, error_response = get_order_or_response(order_no)
    if error_response:
        return error_response
    if not can_access_order(order, request.user):
        return forbidden_response()

    if order.status == Order.STATUS_PENDING_PAYMENT and not order.paid:
        if expire_due_unpaid_order(order):
            order, error_response = get_order_or_response(order_no)
            if error_response:
                return error_response

    if reconcile_pending_virtual_payment(order, request.user):
        order, error_response = get_order_or_response(order_no)
        if error_response:
            return error_response

    data = BossOrderDetailSerializer(order).data
    data.update(payment_deadline_payload(order))
    data['replacement'] = replacement_payload(order)
    data['cancel_reason'] = order.cancel_reason or ''
    data['canceled_at'] = order.canceled_at.isoformat() if order.canceled_at else None
    return Response(data)
