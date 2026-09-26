from rest_framework import serializers


class RechargeCreateSerializer(serializers.Serializer):
    recharge_product_id = serializers.IntegerField(min_value=1, required=False)
    # 兼容旧版小程序一个发布周期；新自由金额充值入口不再使用此序列化器。
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
    # 官方 short_series_coin 充值形成的余额在消费时需要 session_key 用户态签名。
    # 前端每次支付前重新 wx.login；纯历史/人工余额仍允许 code 为空。
    code = serializers.CharField(required=False, allow_blank=True, allow_null=True, default='')
