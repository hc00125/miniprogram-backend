from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.wallet.coin_recharge import (
    coin_recharge_payload,
    latest_checkout_coin_recharge,
    query_coin_recharge,
)

from .models import Payment
from .serializers import PaymentSerializer
from .virtualpay import (
    VIRTUAL_CHANNEL,
    VirtualPaymentAPIError,
    VirtualPaymentConfigurationError,
    VirtualPaymentError,
    query_virtual_payment,
)
from .views import (
    _idempotent_paid_checkout,
    unexpected_virtual_payment_response,
    virtual_payment_error_response,
)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def query_wechat_virtual_by_order_indexed(request, order_no):
    """Recover an interrupted checkout from its durable order/recharge link."""
    paid = _idempotent_paid_checkout(order_no, request.user)
    if paid:
        return Response({'found': True, **paid})

    profile = getattr(request.user, 'client_profile', None)
    recharge = latest_checkout_coin_recharge(profile, order_no) if profile else None
    if recharge:
        try:
            synced = query_coin_recharge(recharge.recharge_no, request.user)
        except (VirtualPaymentConfigurationError, VirtualPaymentAPIError, VirtualPaymentError) as exc:
            return virtual_payment_error_response(exc)
        except Exception as exc:
            return unexpected_virtual_payment_response(
                exc,
                action='query_coin_checkout_by_order',
                request=request,
                order_no=order_no,
                payment_no=recharge.recharge_no,
            )
        data = coin_recharge_payload(synced)
        data['found'] = True
        return Response(data)

    # Compatibility fallback for migration-era short_series_goods Payment rows.
    payments = list(
        Payment.objects
        .select_related('order', 'order__boss_user')
        .filter(
            order__order_no=order_no,
            order__boss_user=request.user,
            channel=VIRTUAL_CHANNEL,
        )
        .order_by('-created_at')[:5]
    )
    if not payments:
        return Response({
            'found': False,
            'order_no': order_no,
            'detail': '该订单暂无可核验的微信虚拟支付单',
        })

    synced_payment = payments[0]
    for candidate in payments:
        try:
            synced_payment = query_virtual_payment(candidate.payment_no, request.user)
        except (VirtualPaymentConfigurationError, VirtualPaymentAPIError, VirtualPaymentError) as exc:
            return virtual_payment_error_response(exc)
        if synced_payment.status == 'paid' or getattr(synced_payment.order, 'paid', False):
            break
    data = dict(PaymentSerializer(synced_payment).data)
    data['found'] = True
    return Response(data)
