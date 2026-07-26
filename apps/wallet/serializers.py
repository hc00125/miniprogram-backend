from rest_framework import serializers


class RechargeCreateSerializer(serializers.Serializer):
    product_id = serializers.IntegerField(min_value=1)
    code = serializers.CharField(required=False, allow_blank=True, allow_null=True, default='')


class BalancePaymentCreateSerializer(serializers.Serializer):
    order_no = serializers.CharField()
