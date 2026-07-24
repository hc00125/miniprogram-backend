import logging

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.payments.models import Payment
from apps.payments.virtualpay import VIRTUAL_CHANNEL, VirtualPaymentError, query_virtual_payment

from .boss_views import can_access_order, forbidden_response, get_order_or_response
from .models import Order
from .payment_deadlines import expire_due_unpaid_order, payment_deadline_payload
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
            # 订单详情仍应正常返回；前端后续刷新时会再次核验。
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

    # 待支付页面每次刷新都兜底检查10分钟支付期限。即使定时任务暂时
    # 没有运行，老板再次进入订单时也会立即释放超时占用的陪玩阵容。
    if order.status == Order.STATUS_PENDING_PAYMENT and not order.paid:
        if expire_due_unpaid_order(order):
            order, error_response = get_order_or_response(order_no)
            if error_response:
                return error_response

    if reconcile_pending_virtual_payment(order, request.user):
        # 支付核验可能已经更新订单状态，重新读取以避免返回旧的“待支付”。
        order, error_response = get_order_or_response(order_no)
        if error_response:
            return error_response

    data = BossOrderDetailSerializer(order).data
    data.update(payment_deadline_payload(order))
    return Response(data)
