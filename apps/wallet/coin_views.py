from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import APIException, ValidationError
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
    VIRTUAL_MODE_COIN,
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
from .ios_credit import IOS_DIAMONDS_PER_YUAN, configured_wallet_credit, decorate_wallet_recharge_payload
from .models import ClientWallet, RechargeOrder, RechargeProduct
from .services import mark_recharge_paid


IOS_RECHARGE_AMOUNTS = {Decimal('10.00'), Decimal('30.00')}


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


def _local_iso(value):
    return timezone.localtime(value).isoformat() if value else None


def _wallet_recharge_payload(recharge):
    stored = dict(recharge.notify_payload or {})
    if stored.get('mode') == VIRTUAL_MODE_COIN:
        base = coin_recharge_payload(recharge)
    else:
        # Historical fixed-product rows may contain cent-level amounts that cannot
        # be represented by today's 10 XPay units/RMB scale. They still need to
        # remain readable in recharge history without being reinterpreted as a
        # modern coin recharge.
        amount = qyuan(recharge.amount)
        base = {
            'recharge_no': recharge.recharge_no,
            'payment_no': recharge.recharge_no,
            'status': recharge.status,
            'amount': str(amount),
            'pay_amount_yuan': str(amount),
            'diamonds': format_diamonds(amount),
            'diamonds_per_yuan': DIAMONDS_PER_YUAN,
            'recharge_product_id': recharge.product_id,
            'order_no': getattr(recharge, 'checkout_order_no', '') or None,
            'checkout_order_no': getattr(recharge, 'checkout_order_no', '') or None,
            'client_platform': stored.get('client_platform', 'other'),
            'platform_fee_percent': stored.get('platform_fee_percent'),
            'estimated_platform_fee_yuan': stored.get('estimated_platform_fee_yuan'),
            'estimated_settlement_yuan': stored.get('estimated_settlement_yuan'),
            'expires_at': _local_iso(recharge.expires_at),
            'created_at': _local_iso(recharge.created_at),
            'paid_at': _local_iso(recharge.paid_at),
            'credited_at': _local_iso(recharge.credited_at),
        }
    return decorate_wallet_recharge_payload(base, recharge)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def recharge_config(request):
    platform = request_client_platform(request)
    minimum, maximum = recharge_limits(platform)
    from apps.earnings.platform_fees import platform_fee_percent, target_net_margin_percent

    fee_percent = platform_fee_percent(platform)

    # iOS keeps only two simple fixed tiers and does not expose free-form recharge.
    # Android/other platforms retain the existing shortcut list and custom amount.
    if platform == 'ios':
        quick_amounts = [10, 30]
        minimum, maximum = Decimal('10.00'), Decimal('30.00')
    else:
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
        credited = configured_wallet_credit(amount, platform, fee_percent)
        results.append({
            'id': legacy_product.id if legacy_product else raw_amount,
            'amount': f'{amount:.2f}',
            'pay_amount_yuan': f'{amount:.2f}',
            'diamonds': format_diamonds(credited),
            'credited_amount_yuan': str(credited),
            'quick_amount': True,
            'legacy_recharge_product_id': legacy_product.id if legacy_product else None,
        })

    one_yuan_credit = configured_wallet_credit(Decimal('1.00'), platform, fee_percent)
    return Response({
        'diamonds_per_yuan': DIAMONDS_PER_YUAN,
        'effective_diamonds_per_yuan': format_diamonds(one_yuan_credit),
        'wechat_coin_units_per_yuan': coin_units_per_yuan(),
        'min_amount_yuan': str(minimum),
        'max_amount_yuan': str(maximum),
        'amount_step_yuan': '10.00' if platform == 'ios' else '0.10',
        'diamond_step': '1.0',
        'client_platform': platform,
        'platform_fee_percent': str(fee_percent),
        'target_net_margin_percent': str(target_net_margin_percent()),
        'fee_deducted_from_credit': platform == 'ios',
        'custom_amount_enabled': platform != 'ios',
        'allowed_amounts_yuan': ['10.00', '30.00'] if platform == 'ios' else None,
        'ios_diamonds_per_yuan': str(IOS_DIAMONDS_PER_YUAN) if platform == 'ios' else None,
        'results': results,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def recharge_create(request):
    serializer = CoinRechargeCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    platform = request_client_platform(request)
    amount = qyuan(serializer.validated_data['amount_yuan'])

    if platform == 'ios' and amount not in IOS_RECHARGE_AMOUNTS:
        raise ValidationError({'amount_yuan': 'iOS仅支持10元或30元固定充值档位，不支持自定义金额'})

    try:
        recharge, payload = create_coin_recharge(
            request.user,
            amount,
            serializer.validated_data.get('code') or '',
            platform,
        )
        if platform == 'ios':
            stored = dict(recharge.notify_payload or {})
            stored['ios_credit_diamonds_per_yuan'] = str(IOS_DIAMONDS_PER_YUAN)
            stored['ios_fixed_credit'] = True
            recharge.notify_payload = stored
            recharge.save(update_fields=['notify_payload', 'updated_at'])
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
    return Response(decorate_wallet_recharge_payload(payload, recharge))


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
    return Response(_wallet_recharge_payload(recharge))


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def recharge_orders(request):
    profile = getattr(request.user, 'client_profile', None)
    if not profile:
        return Response({'detail': '请先微信登录'}, status=status.HTTP_404_NOT_FOUND)
    try:
        page = max(1, int(request.query_params.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(100, max(1, int(request.query_params.get('page_size', 20))))
    except (TypeError, ValueError):
        page_size = 20

    queryset = RechargeOrder.objects.filter(profile=profile).select_related('profile').order_by('-created_at', '-id')
    count = queryset.count()
    start = (page - 1) * page_size
    results = []
    for recharge in queryset[start:start + page_size]:
        payload = _wallet_recharge_payload(recharge)
        if recharge.status == RechargeOrder.STATUS_CREDITED:
            wallet = ClientWallet.objects.filter(profile_id=recharge.profile_id).first()
            if wallet:
                payload['balance_diamonds'] = format_diamonds(wallet.balance)
        results.append(payload)
    return Response({'count': count, 'results': results})


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

    payload = dict(recharge.notify_payload or {})
    payload['mock'] = True
    payload['mock_paid_at'] = timezone.now().isoformat()
    recharge = mark_recharge_paid(recharge, 'mock_paid', payload)
    return Response(_wallet_recharge_payload(recharge))
