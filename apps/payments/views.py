from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import Payment
from .serializers import MiniProgramPaymentCreateSerializer, PaymentCreateSerializer, PaymentSerializer
from .services import create_miniprogram_payment, create_payment, log_callback, mark_payment_paid


@api_view(['POST'])
@permission_classes([AllowAny])
def create(request):
    serializer = PaymentCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    payment = create_payment(serializer.validated_data['order_no'], serializer.validated_data['channel'])
    return Response(PaymentSerializer(payment).data)


@api_view(['POST'])
@permission_classes([AllowAny])
def create_wechat_miniprogram(request):
    serializer = MiniProgramPaymentCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    payment, payload = create_miniprogram_payment(**serializer.validated_data)
    return Response(payload)


@api_view(['GET'])
@permission_classes([AllowAny])
def status_view(request, payment_no):
    payment = Payment.objects.filter(payment_no=payment_no).select_related('order').first()
    if not payment:
        return Response({'detail': '支付单不存在'}, status=status.HTTP_404_NOT_FOUND)
    return Response(PaymentSerializer(payment).data)


@api_view(['POST'])
@permission_classes([AllowAny])
def mock_success(request, payment_no):
    payment = Payment.objects.filter(payment_no=payment_no).select_related('order').first()
    if not payment:
        return Response({'detail': '支付单不存在'}, status=status.HTTP_404_NOT_FOUND)
    payment = mark_payment_paid(payment, 'mock_paid', {'mock': True})
    return Response(PaymentSerializer(payment).data)


@api_view(['POST'])
@permission_classes([AllowAny])
def wechat_callback(request):
    payment_no = request.data.get('out_trade_no') or request.data.get('payment_no') or ''
    log_callback('wechat', request.data, payment_no, verified=False, result='callback received; real verification not configured')
    return Response({'code': 'SUCCESS', 'message': '成功'})
