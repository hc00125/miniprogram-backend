from rest_framework import serializers

from .models import Payment


class PaymentSerializer(serializers.ModelSerializer):
    order_no = serializers.CharField(source='order_id')
    mock = serializers.BooleanField(read_only=True)
    order_status = serializers.CharField(source='order.status', read_only=True)

    class Meta:
        model = Payment
        fields = ['payment_no', 'order_no', 'channel', 'scene', 'amount', 'status', 'qr_code', 'expires_at', 'paid_at', 'third_trade_no', 'order_status', 'mock']


class PaymentCreateSerializer(serializers.Serializer):
    order_no = serializers.CharField()
    channel = serializers.ChoiceField(choices=['wechat', 'alipay'])


class MiniProgramPaymentCreateSerializer(serializers.Serializer):
    order_no = serializers.CharField()
    code = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    openid = serializers.CharField(required=False, allow_blank=True, allow_null=True)
