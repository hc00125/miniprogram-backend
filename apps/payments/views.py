import logging
import uuid

from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import APIException
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import Payment
from .serializers import (
    MiniProgramPaymentCreateSerializer,
    PaymentCreateSerializer,
    PaymentSerializer,
    VirtualPaymentCreateSerializer,
)
from .services import (
    create_miniprogram_payment,
    create_payment,
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
    create_virtual_payment,
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
    """支付前主动核验旧支付单，避免复用微信侧已经关闭的 outTradeNo。"""
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
        logger.warning(
            '[虚拟支付] 支付前核验旧支付单失败，暂不替换 payment_no=%s error=%s',
            payment.payment_no,
            exc,
        )
        return False
    except (VirtualPaymentConfigurationError, VirtualPaymentError) as exc:
        logger.warning(
            '[虚拟支付] 支付前核验旧支付单失败，暂不替换 payment_no=%s error=%s',
            payment.payment_no,
            exc,
        )
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


@api_view(['POST'])
@permission_classes([AllowAny])
def create(request):
    if settings.WECHAT_VIRTUALPAY_ENABLED:
        return ordinary_payment_disabled_response()
    serializer = PaymentCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    payment = create_payment(serializer.validated_data['order_no'], serializer.validated_data['channel'])
    return Response(PaymentSerializer(payment).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_wechat_miniprogram(request):
    if settings.WECHAT_VIRTUALPAY_ENABLED:
        return ordinary_payment_disabled_response()
    serializer = MiniProgramPaymentCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        payment, payload = create_miniprogram_payment(
            user=request.user,
            **serializer.validated_data,
        )
    except WechatPayConfigurationError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except WechatPayAPIError as exc:
        return Response(
            {'detail': str(exc), 'wechat_code': exc.code},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    except WechatPayError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
    return Response(payload)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_wechat_virtual(request):
    serializer = VirtualPaymentCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    order_no = serializer.validated_data.get('order_no', '')
    try:
        reconcile_closed_virtual_payment(order_no, request.user)
        _, payload = create_virtual_payment(
            user=request.user,
            **serializer.validated_data,
        )
    except (VirtualPaymentConfigurationError, VirtualPaymentAPIError, VirtualPaymentError) as exc:
        return virtual_payment_error_response(exc)
    except APIException:
        raise
    except Exception as exc:
        return unexpected_virtual_payment_response(
            exc,
            action='create',
            request=request,
            order_no=order_no,
        )
    logger.info('[虚拟支付] 返回前 payload keys: %s', list(payload.keys()))
    logger.info('[虚拟支付] paySig=%s... signData=%s...', payload.get('paySig', 'MISSING')[:16], payload.get('signData', 'MISSING')[:30])
    return Response(payload)


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
        return Response(
            {'detail': str(exc), 'wechat_code': exc.code},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    except WechatPayError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(PaymentSerializer(payment).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def query_wechat_virtual(request, payment_no):
    try:
        payment = query_virtual_payment(payment_no, request.user)
    except (VirtualPaymentConfigurationError, VirtualPaymentAPIError, VirtualPaymentError) as exc:
        return virtual_payment_error_response(exc)
    except APIException:
        raise
    except Exception as exc:
        return unexpected_virtual_payment_response(
            exc,
            action='query',
            request=request,
            payment_no=payment_no,
        )
    return Response(PaymentSerializer(payment).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def query_wechat_virtual_by_order(request, order_no):
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
        except APIException:
            raise
        except Exception as exc:
            return unexpected_virtual_payment_response(
                exc,
                action='query_by_order',
                request=request,
                order_no=order_no,
                payment_no=candidate.payment_no,
            )

        if synced_payment.status == 'paid' or getattr(synced_payment.order, 'paid', False):
            break

    data = dict(PaymentSerializer(synced_payment).data)
    data['found'] = True
    return Response(data)


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
        return Response(
            {'code': 'FAIL', 'message': '回调报文编码错误'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        handle_wechat_notification(request.headers, raw_body)
    except WechatPaySignatureError as exc:
        log_callback('wechat', {'error': str(exc)}, verified=False, result='signature failed')
        return Response(
            {'code': 'FAIL', 'message': '签名验证失败'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except WechatPayConfigurationError as exc:
        log_callback('wechat', {'error': str(exc)}, verified=False, result='configuration error')
        return Response(
            {'code': 'FAIL', 'message': '服务器支付配置错误'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    except WechatPayError as exc:
        log_callback('wechat', {'error': str(exc)}, verified=True, result='callback rejected')
        return Response(
            {'code': 'FAIL', 'message': str(exc)[:200]},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return Response(status=status.HTTP_204_NO_CONTENT)
