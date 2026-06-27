from rest_framework import serializers

from .models import CartItem, Order, OrderPlayer, Rating


class OrderCreateSerializer(serializers.Serializer):
    boss_wechat = serializers.CharField(max_length=50)
    game_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    package_id = serializers.IntegerField()
    spec_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    required_players = serializers.IntegerField(required=False, min_value=1)
    addon_id = serializers.IntegerField(required=False, allow_null=True)
    addon_details = serializers.ListField(child=serializers.DictField(), required=False, allow_empty=True, allow_null=True)
    designated_players = serializers.ListField(child=serializers.IntegerField(), required=False, allow_empty=True, allow_null=True)
    boss_note = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    booked_hours = serializers.FloatField(required=False, allow_null=True)


class CartItemSerializer(serializers.ModelSerializer):
    package_id = serializers.IntegerField(source='package.id', read_only=True)
    package_name = serializers.CharField(source='package.name', read_only=True)
    group_name = serializers.CharField(source='package.group.name', read_only=True, allow_null=True)
    product_type = serializers.SerializerMethodField()

    class Meta:
        model = CartItem
        fields = [
            'id', 'package_id', 'package_name', 'group_name', 'product_type', 'image_url', 'description',
            'spec_id', 'spec_name', 'spec_display_name', 'price', 'quantity', 'created_at', 'updated_at'
        ]

    def get_product_type(self, obj):
        return getattr(obj.package, 'product_type', None) or 'normal'


class CartItemCreateSerializer(serializers.Serializer):
    package_id = serializers.IntegerField()
    spec_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    spec_name = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=120)
    spec_display_name = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=120)
    price = serializers.FloatField(min_value=0)
    quantity = serializers.IntegerField(required=False, min_value=1, max_value=99)
    image_url = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=500)
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=300)


class CartItemQuantitySerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1, max_value=99)


class OrderPlayerSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source='player.id')
    name = serializers.CharField(source='player.name')
    type_name = serializers.CharField(source='player.player_type.name')

    class Meta:
        model = OrderPlayer
        fields = ['id', 'name', 'type_name', 'is_designated', 'grab_time', 'status']


class BossOrderDetailSerializer(serializers.ModelSerializer):
    package_name = serializers.CharField(source='package.name')
    addon_name = serializers.CharField(source='addon.name', allow_null=True)
    players = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id', 'order_no', 'boss_wechat', 'game_id', 'package_name', 'addon_name', 'addon_details',
            'required_players', 'designated_types', 'designated_players', 'boss_note', 'total_price_per_hour',
            'status', 'start_time', 'end_time', 'duration_minutes', 'total_amount', 'paid', 'is_custom',
            'custom_price', 'created_at', 'booked_hours', 'timer_started_at', 'paused_duration', 'is_paused',
            'last_paused_at', 'players'
        ]

    def get_players(self, obj):
        return OrderPlayerSerializer(obj.order_players.select_related('player__player_type'), many=True).data


class BossOrderListSerializer(serializers.ModelSerializer):
    package_name = serializers.CharField(source='package.name')

    class Meta:
        model = Order
        fields = ['order_no', 'package_name', 'status', 'total_price_per_hour', 'total_amount', 'paid', 'created_at']


class AvailableOrderSerializer(serializers.ModelSerializer):
    package_name = serializers.CharField(source='package.name')
    addon_name = serializers.CharField(source='addon.name', allow_null=True)
    current_players = serializers.SerializerMethodField()
    can_grab = serializers.SerializerMethodField()
    is_designated = serializers.SerializerMethodField()
    designated_type_ids = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'order_no', 'package_name', 'addon_name', 'required_players', 'current_players',
            'total_price_per_hour', 'booked_hours', 'boss_note', 'is_custom', 'can_grab',
            'is_designated', 'designated_type_ids', 'created_at'
        ]

    def get_current_players(self, obj):
        return obj.order_players.count()

    def get_is_designated(self, obj):
        player = self.context.get('player')
        return bool(player and obj.designated_players and player.id in obj.designated_players)

    def get_designated_type_ids(self, obj):
        return [item.get('type_id') for item in (obj.designated_types or []) if item.get('type_id')]

    def get_can_grab(self, obj):
        player = self.context.get('player')
        if not player:
            return False
        return obj.can_player_grab if hasattr(obj, 'can_player_grab') else True


class PlayerOrderListSerializer(serializers.ModelSerializer):
    package_name = serializers.CharField(source='package.name')
    addon_name = serializers.CharField(source='addon.name', allow_null=True)
    grab_time = serializers.SerializerMethodField()
    is_designated = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ['order_no', 'package_name', 'addon_name', 'game_id', 'status', 'start_time', 'end_time', 'duration_minutes', 'grab_time', 'is_designated', 'total_amount', 'total_price_per_hour', 'created_at']

    def _op(self, obj):
        player = self.context.get('player')
        return obj.order_players.filter(player=player).first()

    def get_grab_time(self, obj):
        op = self._op(obj)
        return op.grab_time if op else None

    def get_is_designated(self, obj):
        op = self._op(obj)
        return bool(op and op.is_designated)


class PlayerOrderDetailSerializer(BossOrderDetailSerializer):
    class Meta(BossOrderDetailSerializer.Meta):
        fields = [
            'order_no', 'game_id', 'package_name', 'addon_name', 'required_players', 'boss_note', 'status',
            'total_price_per_hour', 'start_time', 'end_time', 'duration_minutes', 'total_amount', 'booked_hours',
            'timer_started_at', 'paused_duration', 'is_paused', 'last_paused_at', 'is_custom', 'created_at', 'players'
        ]


class OrderActionSerializer(serializers.Serializer):
    order_no = serializers.CharField()
    player_id = serializers.IntegerField(required=False)


class RatingCreateSerializer(serializers.Serializer):
    player_id = serializers.IntegerField()
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(required=False, allow_blank=True, allow_null=True)
