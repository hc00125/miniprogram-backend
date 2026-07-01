from django.contrib import admin

from .models import CartItem, Order, OrderEditLog, OrderItem, OrderPlayer, OrderStatusLog, Rating


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ['package', 'spec', 'package_name', 'spec_name', 'spec_display_name', 'unit_price', 'quantity', 'amount', 'image_url', 'description', 'sort_order']
    can_delete = False


class OrderPlayerInline(admin.TabularInline):
    model = OrderPlayer
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ['id', 'order_no', 'boss_wechat', 'package_name_snapshot', 'status', 'total_amount', 'paid', 'created_at']
    list_filter = ['status', 'paid', 'package']
    search_fields = ['order_no', 'boss_wechat', 'game_id', 'package_name_snapshot', 'items__package_name', 'items__spec_name']
    inlines = [OrderItemInline, OrderPlayerInline]


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ['id', 'order', 'package_name', 'spec_display_name', 'unit_price', 'quantity', 'amount', 'created_at']
    list_filter = ['package']
    search_fields = ['order__order_no', 'package_name', 'spec_name', 'spec_display_name']


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
