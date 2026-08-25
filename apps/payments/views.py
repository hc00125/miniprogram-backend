import logging
import uuid

from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.common.purchase_guard import request_client_platform
from apps.orders.models import Order
from apps.orders.payment_deadlines import ensure_payment_window_open
from apps.wallet.coin_balance_service import pay_order_with_coin_aware_balance
from apps.wallet.coin_recharge import (
    VIRTUAL_MODE_COIN,
    coin_recharge_payload,
    create_coin_recharge,
    query_coin_recharge,
)
from apps.wallet.models import RechargeOrder

from .models import Payment
from .serializers import (
    MiniProgramPaymentCreateSerializer,
    PaymentCreateSerializer,
    PaymentSerializer,
    VirtualPaymentCreateSerializer,
)
from .services import (
    close_payment,
    create_miniprogram_payment,
    create_payment,
    get_order_amount,
    get_payment_for_user,
    handle_wechat_notification,
    log_callback,
    mark_payment_paid,
    query_wechat_payment,
)
from .virtualpay import (
    VIRTUAL_CHANNEL,
    VIRTUAL_MODE_GOODS,
    VirtualPaymentAPIError,
    VirtualPaymentConfigurationError,
    VirtualPaymentError,
    query_virtual_payment,
)
from .wechatpay import (
    WechatPayAPIError,
    WechatPayConfigurationError,
    WechatPayError,
    WechatPaySignatureError,
)

logger = logging.getLogger(__name__)

XPAY_STATUS_CLOSED = 6


def ordinary_payment_disabled_response():
    return Response(
        {'detail': '小程序虚拟商品必须使用官方小程序虚拟支付'},
        status=status.HTTP_410_GONE,
    )


def unexpected_virtual_payment_response(exc, *, action, request, order_no='', payment_no=''):
    error_id = uuid.uuid4().hex[:12]
    logger.exception(
        'Unexpected virtual payment error action=%s error_id=%s user_id=%s order_no=%s payment_no=%s',
        action,
        error_id,
        getattr(request.user, 'id', None),
        order_no,
        payment_no,
        exc_info=exc,
    )
    return Response(
        {
            'detail': f'虚拟支付服务内部错误，请联系管理员并提供错误编号：{error_id}',
            'error_id': error_id,
        },
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def virtual_payment_error_response(exc):
    if isinstance(exc, VirtualPaymentConfigurationError):
        return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    if isinstance(exc, VirtualPaymentAPIError):
        return Response(
            {'detail': str(exc), 'wechat_code': exc.code},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    if isinstance(exc, VirtualPaymentError):
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return None


def _request_ip(request):
    forwarded = str(request.META.get('HTTP_X_FORWARDED_FOR') or '').split(',')[0].strip()
    return forwarded or str(request.META.get('REMOTE_ADDR') or '127.0.0.1')


def _mark_virtual_payment_closed(payment, reason='微信侧订单已关闭'):
    updated = Payment.objects.filter(pk=payment.pk, status='paying').update(
        status='closed',
        updated_at=timezone.now(),
    )
    if updated:
        logger.info(
            '[虚拟支付] 关闭不可复用支付单 payment_no=%s order_no=%s reason=%s',
            payment.payment_no,
            payment.order_id,
            reason,
        )
    return bool(updated)


def reconcile_closed_virtual_payment(order_no, user):
    """兼容迁移前 short_series_goods 支付单，只负责清理微信侧已经关闭的旧单。"""
    payment = (
        Payment.objects
        .select_related('order', 'order__boss_user')
        .filter(
            order__order_no=order_no,
            order__boss_user=user,
            channel=VIRTUAL_CHANNEL,
            scene=VIRTUAL_MODE_GOODS,
            status='paying',
        )
        .order_by('-created_at')
        .first()
    )
    if not payment:
        return False

    try:
        synced = query_virtual_payment(payment.payment_no, user)
    except VirtualPaymentAPIError as exc:
        if 'ORDER_CLOSED' in str(exc).upper():
            return _mark_virtual_payment_closed(payment, reason=str(exc)[:200])
        logger.warning('[虚拟支付] 支付前核验旧支付单失败 payment_no=%s error=%s', payment.payment_no, exc)
        return False
    except (VirtualPaymentConfigurationError, VirtualPaymentError) as exc:
        logger.warning('[虚拟支付] 支付前核验旧支付单失败 payment_no=%s error=%s', payment.payment_no, exc)
        return False

    if synced.status == 'paid' or getattr(synced.order, 'paid', False):
        return False
    order_data = dict((synced.notify_payload or {}).get('query_order') or {})
    try:
        xpay_status = int(order_data.get('status') or 0)
    except (TypeError, ValueError):
        xpay_status = 0
    if xpay_status == XPAY_STATUS_CLOSED:
        return _mark_virtual_payment_closed(synced)
    return False


def _checkout_coin_recharge(payment_no, user):
    recharge = (
        RechargeOrder.objects
        .select_related('profile', 'profile__user')
        .filter(recharge_no=payment_no, profile__user=user)
        .first()
    )
    if not recharge:
        return None
    payload = dict(recharge.notify_payload or {})
    if payload.get('mode') != VIRTUAL_MODE_COIN or not payload.get('checkout_order_no'):
        return None
    return recharge


def _idempotent_paid_checkout(order_no, user):
    order = Order.objects.filter(order_no=order_no, boss_user=user).first()
    if not order or not order.paid:
        return None
    payment = order.payments.filter(status='paid').order_by('-paid_at', '-id').first()
    return {
        'payment_no': payment.payment_no if payment else '',
        'order_no': order.order_no,
        'status': 'paid',
        'order_status': order.status,
        'amount': str(get_order_amount(order)),
    }


@api_view(['POST'])
@permission_classes([AllowAny])
def create(request):
    if settings.WECHAT_VIRTUALPAY_ENABLED:
        return ordinary_payment_disabled_response()
    serializer = PaymentCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    ensure_payment_window_open(serializer.validated_data['order_no'])
    payment = create_payment(serializer.validated_data['order_no'], serializer.validated_data['channel'])
    return Response(PaymentSerializer(payment).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_wechat_miniprogram(request):
    if settings.WECHAT_VIRTUALPAY_ENABLED:
        return ordinary_payment_disabled_response()
    serializer = MiniProgramPaymentCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    ensure_payment_window_open(serializer.validated_data['order_no'], request.user)
    try:
        payment, payload = create_miniprogram_payment(user=request.user, **serializer.validated_data)
    except WechatPayConfigurationError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except WechatPayAPIError as exc:
        return Response({'detail': str(exc), 'wechat_code': exc.code}, status=status.HTTP_502_BAD_GATEWAY)
    except WechatPayError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
    return Response(payload)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_wechat_virtual(request):
    """商城/指定陪练/续单的即时支付统一改为 coin 充值。

    微信只看到“充值 N 钻石”，不再要求每个业务商品绑定 productId。
    充值成功后前端会调用 finalize_checkout_coin，把钻石通过 currency_pay
    扣给对应业务订单并落本地 Payment/账本。
    """
    serializer = VirtualPaymentCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    order_no = serializer.validated_data.get('order_no', '')
    try:
        order = ensure_payment_window_open(order_no, request.user)
        reconcile_closed_virtual_payment(order_no, request.user)
        legacy_paying = Payment.objects.filter(
            order=order,
            channel=VIRTUAL_CHANNEL,
            scene=VIRTUAL_MODE_GOODS,
            status='paying',
        ).exists()
        if legacy_paying:
            raise ValidationError({'detail': '该订单存在迁移前的微信支付单，请取消旧支付后再重试'})

        amount = get_order_amount(order)
        _recharge, payload = create_coin_recharge(
            request.user,
            amount,
            serializer.validated_data['code'],
            request_client_platform(request),
            checkout_order_no=order_no,
        )
    except (VirtualPaymentConfigurationError, VirtualPaymentAPIError, VirtualPaymentError) as exc:
        return virtual_payment_error_response(exc)
    except APIException:
        raise
    except Exception as exc:
        return unexpected_virtual_payment_response(exc, action='create_coin_checkout', request=request, order_no=order_no)
    return Response(payload)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def finalize_checkout_coin(request, recharge_no):
    recharge = _checkout_coin_recharge(recharge_no, request.user)
    if not recharge:
        raise ValidationError({'detail': '订单充值单不存在'})
    order_no = str(dict(recharge.notify_payload or {}).get('checkout_order_no') or '')
    already_paid = _idempotent_paid_checkout(order_no, request.user)
    if already_paid:
        return Response(already_paid)

    code = str(request.data.get('code') or '')
    if not code:
        raise ValidationError({'code': '缺少微信登录凭证，请重新进入小程序后重试'})
    try:
        recharge = query_coin_recharge(recharge_no, request.user)
        if recharge.status != RechargeOrder.STATUS_CREDITED:
            return Response({
                **coin_recharge_payload(recharge),
                'detail': '钻石充值仍在确认中',
            })
        result = pay_order_with_coin_aware_balance(
            order_no,
            request.user,
            code=code,
            user_ip=_request_ip(request),
            allow_credited_checkout_recovery=True,
        )
    except (VirtualPaymentConfigurationError, VirtualPaymentAPIError, VirtualPaymentError) as exc:
        return virtual_payment_error_response(exc)
    except APIException:
        raise
    except Exception as exc:
        return unexpected_virtual_payment_response(
            exc,
            action='finalize_coin_checkout',
            request=request,
            order_no=order_no,
            payment_no=recharge_no,
        )
    result['order_status'] = Order.objects.filter(order_no=order_no).values_list('status', flat=True).first()
    return Response(result)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def status_view(request, payment_no):
    payment = get_payment_for_user(payment_no, request.user)
    return Response(PaymentSerializer(payment).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def query_wechat_order(request, payment_no):
    if settings.WECHAT_VIRTUALPAY_ENABLED:
        return ordinary_payment_disabled_response()
    try:
        payment = query_wechat_payment(payment_no, request.user)
    except WechatPayConfigurationError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except WechatPayAPIError as exc:
        return Response({'detail': str(exc), 'wechat_code': exc.code}, status=status.HTTP_502_BAD_GATEWAY)
    except WechatPayError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(PaymentSerializer(payment).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def query_wechat_virtual(request, payment_no):
    recharge = _checkout_coin_recharge(payment_no, request.user)
    if recharge:
        try:
            recharge = query_coin_recharge(payment_no, request.user)
        except (VirtualPaymentConfigurationError, VirtualPaymentAPIError, VirtualPaymentError) as exc:
            return virtual_payment_error_response(exc)
        except APIException:
            raise
        except Exception as exc:
            return unexpected_virtual_payment_response(exc, action='query_coin_checkout', request=request, payment_no=payment_no)
        return Response(coin_recharge_payload(recharge))

    try:
        payment = query_virtual_payment(payment_no, request.user)
    except (VirtualPaymentConfigurationError, VirtualPaymentAPIError, VirtualPaymentError) as exc:
        return virtual_payment_error_response(exc)
    except APIException:
        raise
    except Exception as exc:
        return unexpected_virtual_payment_response(exc, action='query', request=request, payment_no=payment_no)
    return Response(PaymentSerializer(payment).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def query_wechat_virtual_by_order(request, order_no):
    paid = _idempotent_paid_checkout(order_no, request.user)
    if paid:
        return Response({'found': True, **paid})

    recharge = (
        RechargeOrder.objects
        .filter(profile__user=request.user)
        .order_by('-created_at')
    )
    recharge = next((r for r in recharge[:20] if dict(r.notify_payload or {}).get('checkout_order_no') == order_no), None)
    if recharge:
        try:
            synced = query_coin_recharge(recharge.recharge_no, request.user)
        except (VirtualPaymentConfigurationError, VirtualPaymentAPIError, VirtualPaymentError) as exc:
            return virtual_payment_error_response(exc)
        data = coin_recharge_payload(synced)
        data['found'] = True
        return Response(data)

    payments = list(
        Payment.objects
        .select_related('order', 'order__boss_user')
        .filter(order__order_no=order_no, order__boss_user=request.user, channel=VIRTUAL_CHANNEL)
        .order_by('-created_at')[:5]
    )
    if not payments:
        return Response({'found': False, 'order_no': order_no, 'detail': '该订单暂无可核验的微信虚拟支付单'})

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


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def close_wechat_virtual(request, payment_no):
    recharge = _checkout_coin_recharge(payment_no, request.user)
    if recharge:
        # coin 充值的收银台取消不盲关本地单；同一单可安全重试，避免极端情况下
        # 微信迟到扣款而本地已经 closed，导致无法自动对账入账。
        return Response({'payment_no': recharge.recharge_no, 'status': recharge.status})

    payment = get_payment_for_user(payment_no, request.user)
    if payment.channel != VIRTUAL_CHANNEL:
        raise ValidationError({'detail': '该支付单不是微信虚拟支付'})
    if payment.status == 'paying':
        payment = close_payment(payment, reason='用户取消支付', call_wechat=False)
    return Response({'payment_no': payment.payment_no, 'status': payment.status})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mock_success(request, payment_no):
    if not settings.ENABLE_MOCK_PAYMENT:
        return Response({'detail': '模拟支付未启用'}, status=status.HTTP_404_NOT_FOUND)
    payment = get_payment_for_user(payment_no, request.user)
    payment = mark_payment_paid(payment, 'mock_paid', {'mock': True})
    return Response(PaymentSerializer(payment).data)


@api_view(['POST'])
@permission_classes([AllowAny])
def wechat_callback(request):
    try:
        raw_body = request.body.decode('utf-8')
    except UnicodeDecodeError:
        return Response({'code': 'FAIL', 'message': '回调报文编码错误'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        handle_wechat_notification(request.headers, raw_body)
    except WechatPaySignatureError as exc:
        log_callback('wechat', {'error': str(exc)}, verified=False, result='signature failed')
        return Response({'code': 'FAIL', 'message': '签名验证失败'}, status=status.HTTP_400_BAD_REQUEST)
    except WechatPayConfigurationError as exc:
        log_callback('wechat', {'error': str(exc)}, verified=False, result='configuration error')
        return Response({'code': 'FAIL', 'message': '服务器支付配置错误'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except WechatPayError as exc:
        log_callback('wechat', {'error': str(exc)}, verified=True, result='callback rejected')
        return Response({'code': 'FAIL', 'message': str(exc)[:200]}, status=status.HTTP_400_BAD_REQUEST)

    return Response(status=status.HTTP_204_NO_CONTENT)
