from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .serializers import MiniProgramPaymentCreateSerializer, PaymentCreateSerializer, PaymentSerializer
from .services import (
    create_miniprogram_payment,
    create_payment,
    get_payment_for_user,
    handle_wechat_notification,
    log_callback,
    mark_payment_paid,
    query_wechat_payment,
)
from .wechatpay import (
    WechatPayAPIError,
    WechatPayConfigurationError,
    WechatPayError,
    WechatPaySignatureError,
)


@api_view(['POST'])
@permission_classes([AllowAny])
def create(request):
    serializer = PaymentCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    payment = create_payment(serializer.validated_data['order_no'], serializer.validated_data['channel'])
    return Response(PaymentSerializer(payment).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_wechat_miniprogram(request):
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


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def status_view(request, payment_no):
    payment = get_payment_for_user(payment_no, request.user)
    return Response(PaymentSerializer(payment).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def query_wechat_order(request, payment_no):
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
