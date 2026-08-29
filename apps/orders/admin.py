from django.contrib import admin
from django.core.exceptions import PermissionDenied

from .admin_delete import can_delete_orders_in_admin
from .models import (
    CartItem,
    Order,
    OrderDesignation,
    OrderEditLog,
    OrderItem,
    OrderPlayer,
    OrderPricingLine,
    OrderStatusLog,
    Rating,
)


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = [
        'package', 'spec', 'package_name', 'spec_name', 'spec_display_name',
        'unit_price', 'quantity', 'amount', 'image_url', 'description', 'sort_order',
    ]
    can_delete = False


class OrderPricingLineInline(admin.TabularInline):
    model = OrderPricingLine
    extra = 0
    can_delete = False
    readonly_fields = [
        'source', 'player_id_snapshot', 'player_name_snapshot',
        'billing_player_type_id', 'billing_player_type_name',
        'package_id_snapshot', 'package_name_snapshot', 'spec_id_snapshot',
        'spec_name_snapshot', 'quantity', 'unit_price', 'amount', 'sort_order',
        'created_at',
    ]


class OrderPlayerInline(admin.TabularInline):
    model = OrderPlayer
    extra = 0
    fields = [
        'player', 'is_designated', 'status', 'grab_time', 'room_join_deadline',
        'room_join_status', 'room_join_confirmed_at',
    ]
    readonly_fields = ['player', 'is_designated', 'status', 'grab_time', 'room_join_deadline', 'room_join_confirmed_at']
    can_delete = False


class OrderDesignationInline(admin.TabularInline):
    model = OrderDesignation
    extra = 0
    can_delete = False
    readonly_fields = [
        'player', 'status', 'extra_amount', 'invited_at', 'responded_at', 'expires_at',
    ]


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'order_no', 'order_type', 'parent_order', 'renewal_index', 'boss_wechat',
        'package_name_snapshot', 'status', 'kook_room_number', 'total_amount', 'paid', 'created_at',
    ]
    list_filter = ['order_type', 'status', 'paid', 'package']
    search_fields = [
        'order_no', 'parent_order__order_no', 'boss_wechat', 'game_id', 'package_name_snapshot',
        'items__package_name', 'items__spec_name', 'kook_room_number',
        'designations__player__name', 'order_players__player__name',
    ]
    readonly_fields = ['kook_room_updated_at', 'kook_room_updated_by']
    fieldsets = (
        ('基础信息', {
            'fields': ('order_no', 'order_type', 'parent_order', 'renewal_index', 'boss_user', 'boss_wechat', 'game_id', 'package', 'status')
        }),
        ('KOOK 房间', {
            'fields': ('kook_room_number', 'kook_room_updated_at', 'kook_room_updated_by')
        }),
        ('金额与时间', {
            'fields': ('pricing_mode', 'total_price_per_hour', 'total_amount', 'composition_price_per_hour', 'composition_pricing_error', 'paid', 'payment_method', 'payment_confirmed_at', 'booked_hours', 'timer_started_at', 'start_time', 'end_time', 'duration_minutes')
        }),
        ('订单快照与备注', {
            'fields': ('spec_id', 'package_name_snapshot', 'spec_name_snapshot', 'spec_price_snapshot', 'composition_sku_id', 'composition_key', 'composition_virtual_spec_id', 'addon', 'addon_details', 'required_players', 'designated_types', 'designated_players', 'boss_note', 'is_custom', 'custom_price')
        }),
        ('取消信息', {
            'fields': ('canceled_at', 'cancel_reason'),
            'classes': ('collapse',),
        }),
    )
    inlines = [OrderItemInline, OrderPricingLineInline, OrderPlayerInline, OrderDesignationInline]

    def has_delete_permission(self, request, obj=None):
        # 正式环境禁止物理删除订单；开发环境也只允许超级管理员在显式开关开启后删除。
        return can_delete_orders_in_admin(request)

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not can_delete_orders_in_admin(request):
            # 不仅隐藏详情页删除按钮，也移除列表页默认“删除所选订单”动作。
            actions.pop('delete_selected', None)
        return actions

    def delete_model(self, request, obj):
        # 防御性校验：即使有人手动构造 Admin 删除地址，也不能绕过环境开关。
        if not can_delete_orders_in_admin(request):
            raise PermissionDenied('当前环境禁止删除订单。')
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        # 批量删除与单条删除使用同一套开关，避免两条路径权限不一致。
        if not can_delete_orders_in_admin(request):
            raise PermissionDenied('当前环境禁止批量删除订单。')
        super().delete_queryset(request, queryset)


@admin.register(OrderPlayer)
class OrderPlayerAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'order', 'player', 'is_designated', 'status', 'grab_time',
        'room_join_deadline', 'room_join_status', 'room_join_confirmed_at',
    ]
    list_filter = ['room_join_status', 'is_designated', 'status', 'grab_time']
    search_fields = ['order__order_no', 'player__name']
    actions = ['waive_room_entry_overdue']
    readonly_fields = [
        'order', 'player', 'is_designated', 'designated_type_id', 'grab_time',
        'room_join_deadline', 'room_join_confirmed_at',
    ]

    @admin.action(description='免除选中的进入房间超时记录')
    def waive_room_entry_overdue(self, request, queryset):
        count = queryset.filter(
            room_join_status__in=[OrderPlayer.ROOM_ENTRY_OVERDUE, OrderPlayer.ROOM_ENTRY_LATE_CONFIRMED]
        ).update(room_join_status=OrderPlayer.ROOM_ENTRY_WAIVED)
        self.message_user(request, f'已免除 {count} 条超时记录')


@admin.register(OrderDesignation)
class OrderDesignationAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'order', 'player', 'status', 'extra_amount', 'invited_at',
        'expires_at', 'responded_at',
    ]
    list_filter = ['status', 'invited_at', 'expires_at']
    search_fields = ['order__order_no', 'player__name']
    readonly_fields = [
        'order', 'player', 'status', 'extra_amount', 'invited_at', 'responded_at', 'expires_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        # 指定邀请会在订单删除时级联删除，因此必须与订单使用相同的开发环境删除权限。
        return can_delete_orders_in_admin(request)


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ['id', 'order', 'package_name', 'spec_display_name', 'unit_price', 'quantity', 'amount', 'created_at']
    list_filter = ['package']
    search_fields = ['order__order_no', 'package_name', 'spec_name', 'spec_display_name']


@admin.register(OrderPricingLine)
class OrderPricingLineAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'order', 'source', 'player_name_snapshot', 'billing_player_type_name',
        'package_name_snapshot', 'spec_name_snapshot', 'quantity', 'unit_price', 'amount',
    ]
    list_filter = ['source', 'billing_player_type_name']
    search_fields = ['order__order_no', 'player_name_snapshot', 'package_name_snapshot', 'spec_name_snapshot']


@admin.register(Rating)
class RatingAdmin(admin.ModelAdmin):
    list_display = ['id', 'order', 'player', 'rating', 'created_at']


@admin.register(OrderStatusLog)
class OrderStatusLogAdmin(admin.ModelAdmin):
    list_display = ['id', 'order', 'from_status', 'to_status', 'operator', 'created_at']


@admin.register(OrderEditLog)
class OrderEditLogAdmin(admin.ModelAdmin):
    list_display = ['id', 'order', 'admin', 'field_name', 'created_at']


@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'package', 'spec_name', 'price', 'quantity', 'updated_at']
    list_filter = ['user']
    search_fields = ['user__username', 'package__name', 'spec_name']
