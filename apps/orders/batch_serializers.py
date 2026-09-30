from rest_framework import serializers


class CartBatchOrderCreateSerializer(serializers.Serializer):
    boss_wechat = serializers.CharField(max_length=50)
    game_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    cart_item_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        min_length=1,
        max_length=20,
    )
    boss_note = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    booked_hours = serializers.FloatField(required=False, default=1.0, min_value=0.5, max_value=24)

    def validate_cart_item_ids(self, value):
        ordered_ids = []
        seen = set()
        for item_id in value:
            if item_id in seen:
                continue
            ordered_ids.append(item_id)
            seen.add(item_id)
        if not ordered_ids:
            raise serializers.ValidationError('请选择需要结算的购物车商品')
        return ordered_ids
