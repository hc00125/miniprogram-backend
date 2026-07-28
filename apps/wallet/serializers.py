from rest_framework import serializers


class RechargeCreateSerializer(serializers.Serializer):
    recharge_product_id = serializers.IntegerField(min_value=1, required=False)
    # 兼容旧版小程序一个发布周期；新前端统一提交 recharge_product_id。
    product_id = serializers.IntegerField(min_value=1, required=False)
    code = serializers.CharField(required=False, allow_blank=True, allow_null=True, default='')

    def validate(self, attrs):
        recharge_product_id = attrs.get('recharge_product_id') or attrs.get('product_id')
        if not recharge_product_id:
            raise serializers.ValidationError({'recharge_product_id': '请选择钻石充值档位'})
        attrs['recharge_product_id'] = recharge_product_id
        return attrs


class BalancePaymentCreateSerializer(serializers.Serializer):
    order_no = serializers.CharField()
