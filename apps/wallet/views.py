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

from .models import ClientWallet, ClientWalletLedger, RechargeOrder, RechargeProduct
from .serializers import BalancePaymentCreateSerializer, RechargeCreateSerializer
from .services import (
    create_recharge,
    mark_recharge_paid,
    pay_order_with_balance,
    qmoney,
    query_recharge,
    recharge_status_payload,
)


def _get_request_profile(request):
    return getattr(request.user, 'client_profile', None)


def _missing_profile_response():
    return Response({'detail': '请先微信登录'}, status=status.HTTP_404_NOT_FOUND)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def overview(request):
    profile = _get_request_profile(request)
    if not profile:
        return _missing_profile_response()
    wallet = ClientWallet.objects.filter(profile=profile).first()
    return Response({
        'balance': str(qmoney(wallet.balance if wallet else 0)),
        'recharged_total': str(qmoney(wallet.recharged_total if wallet else 0)),
        'spent_total': str(qmoney(wallet.spent_total if wallet else 0)),
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def recharge_packages(request):
    profile = _get_request_profile(request)
    if not profile:
        return _missing_profile_response()
    products = RechargeProduct.objects.filter(is_active=True).order_by('sort_order', 'id')
    return Response({
        'results': [
            {'id': product.id, 'amount': str(qmoney(product.amount))}
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
        _recharge, payload = create_recharge(
            request.user,
            serializer.validated_data['product_id'],
            serializer.validated_data.get('code') or '',
        )
    except (VirtualPaymentConfigurationError, VirtualPaymentAPIError, VirtualPaymentError) as exc:
        return virtual_payment_error_response(exc)
    except APIException:
        raise
    except Exception as exc:
        return unexpected_virtual_payment_response(exc, action='recharge_create', request=request)
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
    return Response(recharge_status_payload(recharge))


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
                'amount': str(qmoney(entry.amount)),
                'balance_after': str(qmoney(entry.balance_after)),
                'note': entry.note,
                'created_at': timezone.localtime(entry.created_at).isoformat(),
            }
            for entry in entries
        ],
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def recharge_mock_success(request, recharge_no):
    # 模拟支付必须显式启用，且不允许与真实虚拟支付通道并存：
    # 否则真实 wechat_virtual 充值单可被免费确认成余额。
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
    return Response(recharge_status_payload(recharge))


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
        result = pay_order_with_balance(order_no, request.user)
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
    return Response(result)
