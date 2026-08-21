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
from .diamonds import (
    DIAMONDS_PER_YUAN,
    coin_units_per_yuan,
    format_diamonds,
    qyuan,
    yuan_to_coin_units,
)
from .models import RechargeProduct


class CoinRechargeCreateSerializer(serializers.Serializer):
    # New clients send RMB amounts; old fixed-tier clients remain compatible.
    # The published WeChat coin ratio is 1 RMB = 10 integer coins, therefore a
    # cash recharge itself can only be expressed in ¥0.10 increments.
    amount_yuan = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal('0.10'),
        required=False,
    )
    recharge_product_id = serializers.IntegerField(min_value=1, required=False)
    product_id = serializers.IntegerField(min_value=1, required=False)
    code = serializers.CharField(required=False, allow_blank=True, allow_null=True, default='')

    def validate(self, attrs):
        amount = attrs.get('amount_yuan')
        legacy_product_id = attrs.get('recharge_product_id') or attrs.get('product_id')

        if amount is None:
            if not legacy_product_id:
                raise serializers.ValidationError({'amount_yuan': '请输入充值金额'})
            product = RechargeProduct.objects.filter(
                id=legacy_product_id,
                is_active=True,
            ).first()
            if not product:
                raise serializers.ValidationError({'recharge_product_id': '充值档位不存在或已停用'})
            amount = qyuan(product.amount)
            attrs['amount_yuan'] = amount
            attrs['legacy_recharge_product_id'] = product.id

        try:
            yuan_to_coin_units(amount)
        except Exception as exc:
            raise serializers.ValidationError({'amount_yuan': str(exc)}) from exc
        return attrs


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def recharge_config(request):
    platform = request_client_platform(request)
    minimum, maximum = recharge_limits(platform)
    from apps.earnings.platform_fees import platform_fee_percent, target_net_margin_percent

    # These are UI shortcuts only. When a matching legacy RechargeProduct is
    # present, expose its real id so old published clients can still submit it.
    quick_amounts = [10, 30, 50, 100, 200, 500, 1000]
    legacy_products = {
        qyuan(product.amount): product
        for product in RechargeProduct.objects.filter(is_active=True)
    }
    results = []
    for raw_amount in quick_amounts:
        amount = qyuan(raw_amount)
        if amount < minimum or amount > maximum:
            continue
        legacy_product = legacy_products.get(amount)
        results.append({
            'id': legacy_product.id if legacy_product else raw_amount,
            'amount': f'{amount:.2f}',
            'pay_amount_yuan': f'{amount:.2f}',
            'diamonds': format_diamonds(amount),
            'quick_amount': True,
            'legacy_recharge_product_id': legacy_product.id if legacy_product else None,
        })

    return Response({
        'diamonds_per_yuan': DIAMONDS_PER_YUAN,
        'wechat_coin_units_per_yuan': coin_units_per_yuan(),
        'min_amount_yuan': str(minimum),
        'max_amount_yuan': str(maximum),
        'amount_step_yuan': '0.10',
        'diamond_step': '1.0',
        'client_platform': platform,
        'platform_fee_percent': str(platform_fee_percent(platform)),
        'target_net_margin_percent': str(target_net_margin_percent()),
        'results': results,
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
