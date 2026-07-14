from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from apps.common.money import money

from .models import CartItem, Order, OrderItem, OrderPlayer, Rating


class OrderCreateItemSerializer(serializers.Serializer):
    package_id = serializers.IntegerField()
    spec_id = serializers.IntegerField(required=False, allow_null=True)
    quantity = serializers.IntegerField(required=False, default=1, min_value=1, max_value=99)
    spec_display_name = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    image_url = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class OrderCreateSerializer(serializers.Serializer):
    boss_wechat = serializers.CharField(max_length=50)
    game_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    package_id = serializers.IntegerField(required=False, allow_null=True)
    spec_id = serializers.IntegerField(required=False, allow_null=True)
    quantity = serializers.IntegerField(required=False, default=1, min_value=1, max_value=99)
    items = OrderCreateItemSerializer(many=True, required=False, allow_empty=False)
    required_players = serializers.IntegerField(required=False, min_value=1)
    addon_id = serializers.IntegerField(required=False, allow_null=True)
    addon_details = serializers.ListField(child=serializers.DictField(), required=False, allow_empty=True, allow_null=True)
    designated_players = serializers.ListField(child=serializers.IntegerField(), required=False, allow_empty=True, allow_null=True)
    boss_note = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    booked_hours = serializers.FloatField(required=False, allow_null=True)

    def validate(self, attrs):
        if not attrs.get('items') and not attrs.get('package_id'):
            raise serializers.ValidationError({'package_id': '缺少商品信息'})
        return attrs


class OrderRenewalCreateSerializer(serializers.Serializer):
    units = serializers.IntegerField(required=False, default=1, min_value=1, max_value=10)


class OrderKookRoomSerializer(serializers.Serializer):
    kook_room_number = serializers.CharField(max_length=100, trim_whitespace=True)

    def validate_kook_room_number(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('请输入 KOOK 房间号')
        return value


class OrderItemSerializer(serializers.ModelSerializer):
    package_id = serializers.IntegerField(source='package.id', read_only=True)
    spec_id = serializers.IntegerField(source='spec.id', read_only=True, allow_null=True)

    class Meta:
        model = OrderItem
        fields = [
            'id', 'package_id', 'package_name', 'spec_id', 'spec_name', 'spec_display_name',
            'unit_price', 'quantity', 'amount', 'image_url', 'description', 'sort_order'
        ]


class OrderPlayerSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source='player.id')
    name = serializers.CharField(source='player.name')
    type_name = serializers.CharField(source='player.player_type.name')
    avatar_url = serializers.SerializerMethodField()
    room_join_status = serializers.SerializerMethodField()
    room_join_status_text = serializers.SerializerMethodField()
    room_join_remaining_seconds = serializers.SerializerMethodField()
    can_confirm_room_join = serializers.SerializerMethodField()

    class Meta:
        model = OrderPlayer
        fields = [
            'id', 'name', 'type_name', 'avatar_url', 'is_designated', 'grab_time', 'status',
            'room_join_deadline', 'room_join_confirmed_at', 'room_join_status',
            'room_join_status_text', 'room_join_remaining_seconds', 'can_confirm_room_join',
        ]

    def get_avatar_url(self, obj):
        try:
            return obj.player.user.client_profile.avatar_url or None
        except AttributeError:
            return None

    def get_room_join_status(self, obj):
        return obj.refresh_room_join_status()

    def get_room_join_status_text(self, obj):
        obj.refresh_room_join_status()
        return obj.get_room_join_status_display()

    def get_room_join_remaining_seconds(self, obj):
        obj.refresh_room_join_status()
        if not obj.room_join_deadline or obj.room_join_status != OrderPlayer.ROOM_ENTRY_PENDING:
            return 0
        return max(0, int((obj.room_join_deadline - timezone.now()).total_seconds()))

    def get_can_confirm_room_join(self, obj):
        return bool(
            obj.order.status not in {Order.STATUS_COMPLETED, Order.STATUS_CANCELLED}
            and obj.room_join_status in {OrderPlayer.ROOM_ENTRY_PENDING, OrderPlayer.ROOM_ENTRY_OVERDUE}
        )


def order_display_name(obj):
    items = list(getattr(obj, 'prefetched_items', None) or [])
    if not items and hasattr(obj, 'items'):
        try:
            items = list(obj.items.all()[:3])
        except Exception:
            items = []
    if len(items) > 1:
        return f'{items[0].package_name}等{len(items)}件商品'
    if len(items) == 1:
        return items[0].package_name
    return obj.package_name_snapshot or getattr(obj.package, 'name', '')


def renewal_snapshot(obj):
    cached = getattr(obj, '_renewal_snapshot_cache', None)
    if cached is not None:
        return cached

    root = obj.parent_order if obj.order_type == Order.ORDER_TYPE_RENEWAL and obj.parent_order_id else obj
    prefetched = getattr(root, 'prefetched_renewal_orders', None)
    if prefetched is None:
        renewals = list(root.renewal_orders.order_by('renewal_index', 'id'))
    else:
        renewals = list(prefetched)

    paid_renewals = [
        item for item in renewals
        if item.paid and item.status == Order.STATUS_COMPLETED
    ]
    pending = next((
        item for item in renewals
        if not item.paid and item.status == Order.STATUS_PENDING_PAYMENT
    ), None)
    renewal_hours = sum((Decimal(str(item.booked_hours or 0)) for item in paid_renewals), Decimal('0'))
    renewal_amount = sum((money(item.total_amount) for item in paid_renewals), Decimal('0'))
    original_hours = Decimal(str(root.booked_hours or 0))

    cached = {
        'root': root,
        'all': renewals,
        'paid': paid_renewals,
        'pending': pending,
        'renewal_count': len(paid_renewals),
        'renewal_booked_hours': float(renewal_hours),
        'renewal_paid_amount': float(renewal_amount),
        'total_booked_hours': float(original_hours + renewal_hours),
    }
    setattr(obj, '_renewal_snapshot_cache', cached)
    if root is not obj:
        setattr(root, '_renewal_snapshot_cache', cached)
    return cached


class RenewalFieldsMixin(serializers.ModelSerializer):
    parent_order_no = serializers.CharField(source='parent_order.order_no', read_only=True, allow_null=True)
    renewal_count = serializers.SerializerMethodField()
    renewal_booked_hours = serializers.SerializerMethodField()
    renewal_paid_amount = serializers.SerializerMethodField()
    total_booked_hours = serializers.SerializerMethodField()
    pending_renewal_order_no = serializers.SerializerMethodField()
    can_renew = serializers.SerializerMethodField()

    def get_renewal_count(self, obj):
        return renewal_snapshot(obj)['renewal_count']

    def get_renewal_booked_hours(self, obj):
        return renewal_snapshot(obj)['renewal_booked_hours']

    def get_renewal_paid_amount(self, obj):
        return renewal_snapshot(obj)['renewal_paid_amount']

    def get_total_booked_hours(self, obj):
        return renewal_snapshot(obj)['total_booked_hours']

    def get_pending_renewal_order_no(self, obj):
        pending = renewal_snapshot(obj)['pending']
        return pending.order_no if pending else None

    def get_can_renew(self, obj):
        data = renewal_snapshot(obj)
        root = data['root']
        return bool(
            root.order_type == Order.ORDER_TYPE_NORMAL
            and root.paid
            and root.status in {Order.STATUS_READY_TO_START, Order.STATUS_IN_PROGRESS}
            and data['pending'] is None
        )


class BossOrderDetailSerializer(RenewalFieldsMixin):
    package_name = serializers.SerializerMethodField()
    addon_name = serializers.CharField(source='addon.name', allow_null=True)
    players = serializers.SerializerMethodField()
    items = OrderItemSerializer(many=True, read_only=True)
    renewals = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id', 'order_no', 'boss_wechat', 'game_id', 'package_name', 'addon_name', 'addon_details',
            'required_players', 'designated_types', 'designated_players', 'boss_note', 'total_price_per_hour',
            'status', 'start_time', 'end_time', 'duration_minutes', 'total_amount', 'paid', 'is_custom',
            'custom_price', 'created_at', 'booked_hours', 'timer_started_at', 'paused_duration', 'is_paused',
            'last_paused_at', 'players', 'items', 'kook_room_number', 'kook_room_updated_at',
            'spec_id', 'package_name_snapshot', 'spec_name_snapshot', 'spec_price_snapshot',
            'order_type', 'parent_order_no', 'renewal_index', 'renewal_count', 'renewal_booked_hours',
            'renewal_paid_amount', 'total_booked_hours', 'pending_renewal_order_no', 'can_renew', 'renewals',
        ]

    def get_package_name(self, obj):
        return order_display_name(obj)

    def get_players(self, obj):
        return OrderPlayerSerializer(
            obj.order_players.select_related('order', 'player__player_type', 'player__user__client_profile'),
            many=True,
        ).data

    def get_renewals(self, obj):
        return [
            {
                'order_no': item.order_no,
                'renewal_index': item.renewal_index,
                'status': item.status,
                'paid': item.paid,
                'booked_hours': item.booked_hours,
                'total_amount': item.total_amount,
                'created_at': item.created_at,
                'payment_confirmed_at': item.payment_confirmed_at,
            }
            for item in renewal_snapshot(obj)['all']
        ]


class BossOrderListSerializer(RenewalFieldsMixin):
    package_name = serializers.SerializerMethodField()
    item_count = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'order_no', 'package_name', 'item_count', 'status', 'total_price_per_hour',
            'total_amount', 'paid', 'created_at', 'kook_room_number', 'order_type',
            'renewal_count', 'renewal_booked_hours', 'renewal_paid_amount', 'total_booked_hours',
            'pending_renewal_order_no', 'can_renew',
        ]

    def get_package_name(self, obj):
        return order_display_name(obj)

    def get_item_count(self, obj):
        items = getattr(obj, 'prefetched_items', None)
        if items is not None:
            return len(items)
        return obj.items.count() if hasattr(obj, 'items') else 1


class AvailableOrderSerializer(serializers.ModelSerializer):
    package_name = serializers.SerializerMethodField()
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
            'is_designated', 'designated_type_ids', 'created_at', 'kook_room_number'
        ]

    def get_package_name(self, obj):
        return order_display_name(obj)

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


class PlayerOrderListSerializer(RenewalFieldsMixin):
    package_name = serializers.SerializerMethodField()
    addon_name = serializers.CharField(source='addon.name', allow_null=True)
    grab_time = serializers.SerializerMethodField()
    is_designated = serializers.SerializerMethodField()
    room_join_deadline = serializers.SerializerMethodField()
    room_join_status = serializers.SerializerMethodField()
    room_join_status_text = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'order_no', 'package_name', 'addon_name', 'game_id', 'status', 'start_time',
            'end_time', 'duration_minutes', 'grab_time', 'is_designated', 'total_amount',
            'total_price_per_hour', 'created_at', 'kook_room_number', 'order_type',
            'renewal_count', 'total_booked_hours', 'pending_renewal_order_no', 'can_renew',
            'room_join_deadline', 'room_join_status', 'room_join_status_text',
        ]

    def get_package_name(self, obj):
        return order_display_name(obj)

    def _op(self, obj):
        cached = getattr(obj, '_current_player_op_cache', None)
        if cached is not None:
            return cached
        player = self.context.get('player')
        op = obj.order_players.filter(player=player).first()
        setattr(obj, '_current_player_op_cache', op)
        return op

    def get_grab_time(self, obj):
        op = self._op(obj)
        return op.grab_time if op else None

    def get_is_designated(self, obj):
        op = self._op(obj)
        return bool(op and op.is_designated)

    def get_room_join_deadline(self, obj):
        op = self._op(obj)
        return op.room_join_deadline if op else None

    def get_room_join_status(self, obj):
        op = self._op(obj)
        return op.refresh_room_join_status() if op else None

    def get_room_join_status_text(self, obj):
        op = self._op(obj)
        if not op:
            return ''
        op.refresh_room_join_status()
        return op.get_room_join_status_display()


class PlayerOrderDetailSerializer(BossOrderDetailSerializer):
    class Meta(BossOrderDetailSerializer.Meta):
        fields = [
            'order_no', 'game_id', 'package_name', 'addon_name', 'required_players', 'boss_note', 'status',
            'total_price_per_hour', 'start_time', 'end_time', 'duration_minutes', 'total_amount', 'booked_hours',
            'timer_started_at', 'paused_duration', 'is_paused', 'last_paused_at', 'is_custom', 'created_at', 'players', 'items',
            'kook_room_number', 'kook_room_updated_at',
            'spec_id', 'package_name_snapshot', 'spec_name_snapshot', 'spec_price_snapshot',
            'order_type', 'parent_order_no', 'renewal_index', 'renewal_count', 'renewal_booked_hours',
            'renewal_paid_amount', 'total_booked_hours', 'pending_renewal_order_no', 'can_renew', 'renewals',
        ]


class OrderActionSerializer(serializers.Serializer):
    order_no = serializers.CharField()
    player_id = serializers.IntegerField(required=False)


class RatingCreateSerializer(serializers.Serializer):
    player_id = serializers.IntegerField()
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class CartItemSerializer(serializers.ModelSerializer):
    package_id = serializers.IntegerField(source='package.id')
    package_name = serializers.CharField(source='package.name')
    group_name = serializers.CharField(source='package.group.name', allow_null=True)
    product_type = serializers.CharField(source='package.product_type', allow_null=True)
    spec_id = serializers.IntegerField(source='spec.id', allow_null=True, read_only=True)

    class Meta:
        model = CartItem
        fields = [
            'id', 'package_id', 'package_name', 'group_name', 'product_type',
            'image_url', 'description',
            'spec_id', 'spec_id_snapshot', 'spec_name', 'spec_display_name',
            'price', 'quantity',
            'created_at', 'updated_at',
        ]


class CartItemCreateSerializer(serializers.Serializer):
    package_id = serializers.IntegerField()
    spec_id = serializers.IntegerField(required=False, allow_null=True)
    spec_name = serializers.CharField(required=False, allow_blank=True, default='')
    spec_display_name = serializers.CharField(required=False, allow_blank=True, default='')
    price = serializers.FloatField(required=False, allow_null=True)
    quantity = serializers.IntegerField(required=False, default=1, min_value=1)
    image_url = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class CartItemQuantitySerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1)
