from decimal import Decimal

from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import APIException
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.purchase_guard import request_client_platform
from apps.payments.views import unexpected_virtual_payment_response, virtual_payment_error_response
from apps.payments.virtualpay import (
    VirtualPaymentAPIError,
    VirtualPaymentConfigurationError,
    VirtualPaymentError,
)

from .coin_recharge import (
    coin_recharge_payload,
    create_coin_recharge,
    query_coin_recharge,
    recharge_limits,
)
from .diamonds import DIAMONDS_PER_YUAN


class CoinRechargeCreateSerializer(serializers.Serializer):
    amount_yuan = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal('0.01'),
    )
    code = serializers.CharField(required=False, allow_blank=True, allow_null=True, default='')

    def validate_amount_yuan(self, value):
        diamonds = value * Decimal(str(DIAMONDS_PER_YUAN))
        if diamonds != diamonds.to_integral_value():
            raise serializers.ValidationError('金额必须为0.1元的整数倍，不能产生小数钻石')
        return value


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def recharge_config(request):
    platform = request_client_platform(request)
    minimum, maximum = recharge_limits(platform)
    from apps.earnings.platform_fees import platform_fee_percent, target_net_margin_percent

    quick_amounts = [10, 30, 50, 100, 200, 500]
    return Response({
        'diamonds_per_yuan': DIAMONDS_PER_YUAN,
        'min_amount_yuan': str(minimum),
        'max_amount_yuan': str(maximum),
        'amount_step_yuan': '0.10',
        'client_platform': platform,
        'platform_fee_percent': str(platform_fee_percent(platform)),
        'target_net_margin_percent': str(target_net_margin_percent()),
        # 保留 results 兼容旧客户端；这些只是快捷金额，不再绑定微信商品ID。
        'results': [
            {
                'id': amount,
                'amount': f'{amount:.2f}',
                'pay_amount_yuan': f'{amount:.2f}',
                'diamonds': amount * DIAMONDS_PER_YUAN,
                'quick_amount': True,
            }
            for amount in quick_amounts
            if Decimal(amount) >= minimum and Decimal(amount) <= maximum
        ],
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def recharge_create(request):
    serializer = CoinRechargeCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    platform = request_client_platform(request)
    try:
        _recharge, payload = create_coin_recharge(
            request.user,
            serializer.validated_data['amount_yuan'],
            serializer.validated_data.get('code') or '',
            platform,
        )
    except (VirtualPaymentConfigurationError, VirtualPaymentAPIError, VirtualPaymentError) as exc:
        return virtual_payment_error_response(exc)
    except APIException:
        raise
    except Exception as exc:
        return unexpected_virtual_payment_response(
            exc,
            action='coin_recharge_create',
            request=request,
        )
    return Response(payload)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def recharge_query(request, recharge_no):
    try:
        recharge = query_coin_recharge(recharge_no, request.user)
    except (VirtualPaymentConfigurationError, VirtualPaymentAPIError, VirtualPaymentError) as exc:
        return virtual_payment_error_response(exc)
    except APIException:
        raise
    except Exception as exc:
        return unexpected_virtual_payment_response(
            exc,
            action='coin_recharge_query',
            request=request,
            payment_no=recharge_no,
        )
    return Response(coin_recharge_payload(recharge))
