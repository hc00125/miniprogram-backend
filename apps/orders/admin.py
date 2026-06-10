from django.contrib import admin

from .models import Order, OrderEditLog, OrderPlayer, OrderStatusLog, Rating


class OrderPlayerInline(admin.TabularInline):
    model = OrderPlayer
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ['id', 'order_no', 'boss_wechat', 'package', 'status', 'total_amount', 'paid', 'created_at']
    list_filter = ['status', 'paid', 'package']
    search_fields = ['order_no', 'boss_wechat', 'game_id']
    inlines = [OrderPlayerInline]


@admin.register(Rating)
class RatingAdmin(admin.ModelAdmin):
    list_display = ['id', 'order', 'player', 'rating', 'created_at']


@admin.register(OrderStatusLog)
class OrderStatusLogAdmin(admin.ModelAdmin):
    list_display = ['id', 'order', 'from_status', 'to_status', 'operator', 'created_at']


@admin.register(OrderEditLog)
class OrderEditLogAdmin(admin.ModelAdmin):
    list_display = ['id', 'order', 'admin', 'field_name', 'created_at']
