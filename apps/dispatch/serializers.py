from rest_framework import serializers


class StrictInput(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError({'detail': '请求包含不支持的字段'})
        return super().to_internal_value(data)


class CancelInput(StrictInput):
    reason = serializers.CharField(max_length=200, allow_blank=False, trim_whitespace=True)


class CustomerInput(StrictInput):
    nickname = serializers.CharField(max_length=100)
    note = serializers.CharField(max_length=300, required=False, default='', allow_blank=True)


class QuoteInput(StrictInput):
    customer_ref = serializers.RegexField(r'^(customer|user):[1-9][0-9]{0,17}$', max_length=30)
    package_id = serializers.IntegerField(min_value=1, max_value=9223372036854775807)
    spec_id = serializers.IntegerField(min_value=1, max_value=9223372036854775807, required=False, allow_null=True)
    hours = serializers.IntegerField(min_value=1, max_value=24)
    game_id = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    boss_note = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')


class OrderInput(QuoteInput):
    quote_version = serializers.RegexField(r'^[a-f0-9]{64}$')
    received = serializers.BooleanField()
    receipt_note = serializers.CharField(max_length=200)
    request_key = serializers.UUIDField()

    def validate_received(self, value):
        if value is not True:
            raise serializers.ValidationError('请先核实线下收款')
        return value
