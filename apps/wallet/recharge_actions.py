from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.payments.views import virtual_payment_error_response
from apps.payments.virtualpay import (
    VirtualPaymentAPIError,
    VirtualPaymentConfigurationError,
    VirtualPaymentError,
)

from .coin_recharge import VIRTUAL_MODE_COIN, query_coin_recharge
from .diamonds import DIAMONDS_PER_YUAN, qyuan, yuan_to_diamonds
from .models import RechargeOrder
from .services import query_recharge as query_legacy_recharge


RECHARGE_CANCEL_FALLBACK_MINUTES = 10


def _is_expired(recharge, now=None):
    now = now or timezone.now()
    expires_at = recharge.expires_at
    if not expires_at:
        expires_at = recharge.created_at + timedelta(minutes=RECHARGE_CANCEL_FALLBACK_MINUTES)
    return expires_at <= now


def _is_coin(recharge):
    return dict(recharge.notify_payload or {}).get('mode') == VIRTUAL_MODE_COIN


def _query_current_status(recharge, user):
    if _is_coin(recharge):
        return query_coin_recharge(recharge.recharge_no, user)
    return query_legacy_recharge(recharge.recharge_no, user)


def _payload(recharge):
    stored = dict(recharge.notify_payload or {})
    return {
        'recharge_no': recharge.recharge_no,
        'status': recharge.status,
        'pay_amount_yuan': str(qyuan(recharge.amount)),
        'diamonds': yuan_to_diamonds(recharge.amount),
        'diamonds_per_yuan': DIAMONDS_PER_YUAN,
        'checkout_order_no': stored.get('checkout_order_no'),
        'created_at': timezone.localtime(recharge.created_at).isoformat(),
        'expires_at': timezone.localtime(recharge.expires_at).isoformat() if recharge.expires_at else None,
    }


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def cancel_recharge(request, recharge_no):
    """Close an unpaid recharge order owned by the current user.

    Coin recharges are queried through the coin-specific path, so cancellation
    can never accidentally invoke notify_provide_goods (cash-goods delivery).
    """
    recharge = (
        RechargeOrder.objects
        .select_related('profile', 'profile__user')
        .filter(recharge_no=recharge_no, profile__user=request.user)
        .first()
    )
    if not recharge:
        return Response({'detail': '充值单不存在'}, status=status.HTTP_404_NOT_FOUND)

    if recharge.status in {RechargeOrder.STATUS_PAID, RechargeOrder.STATUS_CREDITED}:
        return Response(
            {'detail': '该充值单已付款，不能取消', **_payload(recharge)},
            status=status.HTTP_409_CONFLICT,
        )
    if recharge.status in {RechargeOrder.STATUS_CLOSED, RechargeOrder.STATUS_FAILED}:
        return Response(_payload(recharge))

    expired = _is_expired(recharge)
    if recharge.channel == RechargeOrder.CHANNEL_WECHAT_VIRTUAL:
        try:
            recharge = _query_current_status(recharge, request.user)
        except (VirtualPaymentConfigurationError, VirtualPaymentAPIError, VirtualPaymentError) as exc:
            if not expired:
                return virtual_payment_error_response(exc)
        if recharge.status in {RechargeOrder.STATUS_PAID, RechargeOrder.STATUS_CREDITED}:
            return Response(
                {'detail': '微信已确认付款，不能取消', **_payload(recharge)},
                status=status.HTTP_409_CONFLICT,
            )

    with transaction.atomic():
        locked = RechargeOrder.objects.select_for_update().get(pk=recharge.pk)
        if locked.status in {RechargeOrder.STATUS_PAID, RechargeOrder.STATUS_CREDITED}:
            return Response(
                {'detail': '该充值单已付款，不能取消', **_payload(locked)},
                status=status.HTTP_409_CONFLICT,
            )
        if locked.status not in {RechargeOrder.STATUS_CLOSED, RechargeOrder.STATUS_FAILED}:
            notify_payload = dict(locked.notify_payload or {})
            notify_payload['cancelled_by_user'] = True
            notify_payload['cancelled_at'] = timezone.now().isoformat()
            locked.notify_payload = notify_payload
            locked.status = RechargeOrder.STATUS_CLOSED
            locked.save(update_fields=['status', 'notify_payload', 'updated_at'])

    return Response(_payload(locked))
