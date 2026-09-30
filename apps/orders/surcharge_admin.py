from django.contrib import admin
from .surcharge_models import OrderSurcharge


@admin.register(OrderSurcharge)
class OrderSurchargeAdmin(admin.ModelAdmin):
    list_display = ('surcharge_no', 'order', 'status', 'amount_diamonds', 'refunded_diamonds', 'created_at')
    list_filter = ('status',)
    search_fields = ('surcharge_no', 'order__order_no')
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
