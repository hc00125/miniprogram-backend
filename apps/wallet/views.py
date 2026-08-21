from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.payments.views import unexpected_virtual_payment_response, virtual_payment_error_response
from apps.payments.virtualpay import (
    VirtualPaymentAPIError,
    VirtualPaymentConfigurationError,
    VirtualPaymentError,
)

from .coin_balance_service import pay_order_with_coin_aware_balance
from .diamonds import DIAMONDS_PER_YUAN, qyuan, yuan_to_diamonds
from .models import ALLOWED_RECHARGE_AMOUNTS, ClientWallet, ClientWalletLedger, RechargeOrder, RechargeProduct
from .serializers import BalancePaymentCreateSerializer, RechargeCreateSerializer
from .services import (
    create_recharge,
    mark_recharge_paid,
    qmoney,
    query_recharge,
    recharge_status_payload,
)


def _get_request_profile(request):
    return getattr(request.user, 'client_profile', None)


def _missing_profile_response():
    return Response({'detail': '请先微信登录'}, status=status.HTTP_404_NOT_FOUND)


def _local_iso(value):
    return timezone.localtime(value).isoformat() if value else None


def _request_ip(request):
    forwarded = str(request.META.get('HTTP_X_FORWARDED_FOR') or '').split(',')[0].strip()
    return forwarded or str(request.META.get('REMOTE_ADDR') or '127.0.0.1')


def _recharge_payload(recharge):
    payload = recharge_status_payload(recharge)
    payload.update({
        'pay_amount_yuan': str(qyuan(recharge.amount)),
        'diamonds': yuan_to_diamonds(recharge.amount),
        'diamonds_per_yuan': DIAMONDS_PER_YUAN,
        'created_at': _local_iso(recharge.created_at),
        'paid_at': _local_iso(recharge.paid_at),
        'credited_at': _local_iso(recharge.credited_at),
    })
    if recharge.status == RechargeOrder.STATUS_CREDITED:
        wallet = ClientWallet.objects.filter(profile_id=recharge.profile_id).first()
        payload['balance_diamonds'] = yuan_to_diamonds(wallet.balance if wallet else 0)
    return payload


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def overview(request):
    profile = _get_request_profile(request)
    if not profile:
        return _missing_profile_response()
    wallet = ClientWallet.objects.filter(profile=profile).first()
    balance = qmoney(wallet.balance if wallet else 0)
    recharged_total = qmoney(wallet.recharged_total if wallet else 0)
    spent_total = qmoney(wallet.spent_total if wallet else 0)
    return Response({
        'balance': str(balance),
        'recharged_total': str(recharged_total),
        'spent_total': str(spent_total),
        'balance_yuan': str(balance),
        'recharged_total_yuan': str(recharged_total),
        'spent_total_yuan': str(spent_total),
        'balance_diamonds': yuan_to_diamonds(balance),
        'recharged_total_diamonds': yuan_to_diamonds(recharged_total),
        'spent_total_diamonds': yuan_to_diamonds(spent_total),
        'diamonds_per_yuan': DIAMONDS_PER_YUAN,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def recharge_packages(request):
    profile = _get_request_profile(request)
    if not profile:
        return _missing_profile_response()
    products = (
        RechargeProduct.objects
        .filter(is_active=True, amount__in=ALLOWED_RECHARGE_AMOUNTS)
        .order_by('sort_order', 'id')
    )
    return Response({
        'diamonds_per_yuan': DIAMONDS_PER_YUAN,
        'results': [
            {
                'id': product.id,
                'amount': str(qmoney(product.amount)),
                'pay_amount_yuan': str(qmoney(product.amount)),
                'diamonds': product.diamond_amount,
            }
            for product in products
        ],
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def recharge_create(request):
    profile = _get_request_profile(request)
    if not profile:
        return _missing_profile_response()
    serializer = RechargeCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        recharge, payload = create_recharge(
            request.user,
            serializer.validated_data['recharge_product_id'],
            serializer.validated_data.get('code') or '',
        )
    except (VirtualPaymentConfigurationError, VirtualPaymentAPIError, VirtualPaymentError) as exc:
        return virtual_payment_error_response(exc)
    except APIException:
        raise
    except Exception as exc:
        return unexpected_virtual_payment_response(exc, action='recharge_create', request=request)
    payload.update({
        'pay_amount_yuan': str(qyuan(recharge.amount)),
        'diamonds': yuan_to_diamonds(recharge.amount),
        'diamonds_per_yuan': DIAMONDS_PER_YUAN,
    })
    return Response(payload)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def recharge_query(request, recharge_no):
    try:
        recharge = query_recharge(recharge_no, request.user)
    except (VirtualPaymentConfigurationError, VirtualPaymentAPIError, VirtualPaymentError) as exc:
        return virtual_payment_error_response(exc)
    except APIException:
        raise
    except Exception as exc:
        return unexpected_virtual_payment_response(
            exc,
            action='recharge_query',
            request=request,
            payment_no=recharge_no,
        )
    return Response(_recharge_payload(recharge))


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def recharge_orders(request):
    profile = _get_request_profile(request)
    if not profile:
        return _missing_profile_response()
    try:
        page = max(1, int(request.query_params.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(100, max(1, int(request.query_params.get('page_size', 20))))
    except (TypeError, ValueError):
        page_size = 20
    queryset = RechargeOrder.objects.filter(profile=profile).order_by('-created_at', '-id')
    count = queryset.count()
    start = (page - 1) * page_size
    return Response({
        'count': count,
        'results': [_recharge_payload(item) for item in queryset[start:start + page_size]],
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def transactions(request):
    profile = _get_request_profile(request)
    if not profile:
        return _missing_profile_response()

    try:
        page = max(1, int(request.query_params.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.query_params.get('page_size', 20))
    except (TypeError, ValueError):
        page_size = 20
    page_size = min(max(1, page_size), 100)

    queryset = (
        ClientWalletLedger.objects
        .filter(wallet__profile=profile)
        .exclude(entry_type__in=ClientWalletLedger.INTERNAL_ENTRY_TYPES)
        .order_by('-created_at', '-id')
    )
    count = queryset.count()
    start = (page - 1) * page_size
    entries = queryset[start:start + page_size]
    return Response({
        'count': count,
        'results': [
            {
                'id': entry.id,
                'entry_type': entry.entry_type,
                'entry_type_display': entry.get_entry_type_display(),
                'amount': str(qmoney(entry.amount)),
                'balance_after': str(qmoney(entry.balance_after)),
                'amount_diamonds': yuan_to_diamonds(entry.amount),
                'balance_after_diamonds': yuan_to_diamonds(entry.balance_after),
                'note': entry.note,
                'created_at': timezone.localtime(entry.created_at).isoformat(),
            }
            for entry in entries
        ],
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def recharge_mock_success(request, recharge_no):
    if not settings.ENABLE_MOCK_PAYMENT or settings.WECHAT_VIRTUALPAY_ENABLED:
        return Response({'detail': '模拟支付未启用'}, status=status.HTTP_404_NOT_FOUND)
    recharge = (
        RechargeOrder.objects
        .select_related('profile')
        .filter(recharge_no=recharge_no, profile__user=request.user)
        .first()
    )
    if not recharge:
        raise ValidationError({'detail': '充值单不存在'})
    if recharge.channel != RechargeOrder.CHANNEL_MOCK:
        raise ValidationError({'detail': '该充值单不支持模拟支付'})
    recharge = mark_recharge_paid(recharge, 'mock_paid', {'mock': True})
    return Response(_recharge_payload(recharge))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def pay_balance_create(request):
    profile = _get_request_profile(request)
    if not profile:
        return _missing_profile_response()
    serializer = BalancePaymentCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    order_no = serializer.validated_data['order_no']
    try:
        result = pay_order_with_coin_aware_balance(
            order_no,
            request.user,
            code=serializer.validated_data.get('code') or '',
            user_ip=_request_ip(request),
        )
    except (VirtualPaymentConfigurationError, VirtualPaymentAPIError, VirtualPaymentError) as exc:
        return virtual_payment_error_response(exc)
    except APIException:
        raise
    except Exception as exc:
        return unexpected_virtual_payment_response(
            exc,
            action='balance_pay',
            request=request,
            order_no=order_no,
        )
    result.update({
        'amount_yuan': str(qyuan(result.get('amount'))),
        'amount_diamonds': yuan_to_diamonds(result.get('amount')),
        'balance_yuan': str(qyuan(result.get('balance'))),
        'balance_diamonds': yuan_to_diamonds(result.get('balance')),
        'diamonds_per_yuan': DIAMONDS_PER_YUAN,
    })
    return Response(result)
