from decimal import Decimal

from rest_framework import serializers

from apps.wallet.diamonds import DIAMONDS_PER_YUAN

from .models import Payment, PaymentCallbackLog, Refund


class PaymentSerializer(serializers.ModelSerializer):
    order_no = serializers.CharField(source='order_id')
    mock = serializers.BooleanField(read_only=True)
    order_status = serializers.CharField(source='order.status', read_only=True)
    amount_yuan = serializers.DecimalField(source='amount', max_digits=12, decimal_places=2, read_only=True)
    amount_diamonds = serializers.IntegerField(read_only=True)
    diamonds_per_yuan = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            'payment_no', 'order_no', 'channel', 'scene', 'amount', 'amount_yuan',
            'amount_diamonds', 'diamonds_per_yuan', 'status', 'qr_code', 'expires_at',
            'paid_at', 'third_trade_no', 'order_status', 'mock',
        ]

    def get_diamonds_per_yuan(self, _obj):
        return DIAMONDS_PER_YUAN


class PaymentAdminSerializer(PaymentSerializer):
    third_order_no = serializers.CharField(read_only=True)
    notify_payload = serializers.JSONField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    class Meta(PaymentSerializer.Meta):
        fields = PaymentSerializer.Meta.fields + ['third_order_no', 'notify_payload', 'created_at', 'updated_at']


class PaymentCallbackLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentCallbackLog
        fields = ['id', 'payment_no', 'channel', 'payload', 'verify_result', 'handled_result', 'created_at']


class PaymentCreateSerializer(serializers.Serializer):
    order_no = serializers.CharField()
    channel = serializers.ChoiceField(choices=['wechat', 'alipay'])


class MiniProgramPaymentCreateSerializer(serializers.Serializer):
    order_no = serializers.CharField()
    code = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    openid = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class VirtualPaymentCreateSerializer(serializers.Serializer):
    order_no = serializers.CharField()
    code = serializers.CharField(allow_blank=False)


class RefundSerializer(serializers.ModelSerializer):
    payment_no = serializers.CharField(source='payment.payment_no', read_only=True)
    order_no = serializers.CharField(source='order_id', read_only=True)
    amount_yuan = serializers.DecimalField(source='amount', max_digits=12, decimal_places=2, read_only=True)
    amount_diamonds = serializers.IntegerField(read_only=True)
    diamonds_per_yuan = serializers.SerializerMethodField()

    class Meta:
        model = Refund
        fields = [
            'refund_no', 'payment_no', 'order_no', 'amount', 'amount_yuan',
            'amount_diamonds', 'diamonds_per_yuan', 'reason', 'status',
            'third_refund_no', 'notify_payload', 'failed_reason', 'created_at', 'updated_at',
        ]

    def get_diamonds_per_yuan(self, _obj):
        return DIAMONDS_PER_YUAN


class RefundCreateSerializer(serializers.Serializer):
    payment_no = serializers.CharField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.01'))
    reason = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=200)
