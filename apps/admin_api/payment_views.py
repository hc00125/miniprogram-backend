from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.common.permissions import IsAdminUser
from apps.payments.models import Payment, PaymentCallbackLog, Refund
from apps.payments.serializers import PaymentAdminSerializer, PaymentCallbackLogSerializer, RefundCreateSerializer, RefundSerializer
from apps.payments.services import create_refund


@api_view(['GET'])
@permission_classes([IsAdminUser])
def payment_transactions(request):
    qs = Payment.objects.select_related('order').all()
    for field in ['status', 'channel', 'scene']:
        value = request.query_params.get(field)
        if value:
            qs = qs.filter(**{field: value})
    order_no = request.query_params.get('order_no')
    if order_no:
        qs = qs.filter(order_id=order_no)
    payment_no = request.query_params.get('payment_no')
    if payment_no:
        qs = qs.filter(payment_no=payment_no)
    return Response(PaymentAdminSerializer(qs[:100], many=True).data)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def payment_detail(request, payment_no):
    payment = Payment.objects.filter(payment_no=payment_no).select_related('order').first()
    if not payment:
        return Response({'detail': '支付单不存在'}, status=status.HTTP_404_NOT_FOUND)
    return Response(PaymentAdminSerializer(payment).data)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def payment_callbacks(request):
    qs = PaymentCallbackLog.objects.all()
    payment_no = request.query_params.get('payment_no')
    if payment_no:
        qs = qs.filter(payment_no=payment_no)
    channel = request.query_params.get('channel')
    if channel:
        qs = qs.filter(channel=channel)
    verified = request.query_params.get('verified')
    if verified in {'true', 'false'}:
        qs = qs.filter(verify_result=(verified == 'true'))
    return Response(PaymentCallbackLogSerializer(qs[:100], many=True).data)


@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def refunds(request):
    if request.method == 'GET':
        qs = Refund.objects.select_related('payment', 'order').all()
        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        payment_no = request.query_params.get('payment_no')
        if payment_no:
            qs = qs.filter(payment__payment_no=payment_no)
        order_no = request.query_params.get('order_no')
        if order_no:
            qs = qs.filter(order_id=order_no)
        return Response(RefundSerializer(qs[:100], many=True).data)

    serializer = RefundCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    refund = create_refund(
        serializer.validated_data['payment_no'],
        serializer.validated_data['amount'],
        serializer.validated_data.get('reason') or '',
        request.user,
    )
    return Response(RefundSerializer(refund).data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def refund_detail(request, refund_no):
    refund = Refund.objects.filter(refund_no=refund_no).select_related('payment', 'order').first()
    if not refund:
        return Response({'detail': '退款单不存在'}, status=status.HTTP_404_NOT_FOUND)
    return Response(RefundSerializer(refund).data)
