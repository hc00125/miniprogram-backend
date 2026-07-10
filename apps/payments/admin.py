from django.contrib import admin

from .models import Payment, PaymentCallbackLog, VirtualProductBinding


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['id', 'payment_no', 'order', 'channel', 'scene', 'amount', 'status', 'created_at']
    list_filter = ['channel', 'scene', 'status']
    search_fields = ['payment_no', 'order__order_no', 'third_trade_no']


@admin.register(VirtualProductBinding)
class VirtualProductBindingAdmin(admin.ModelAdmin):
    list_display = ['id', 'product_id', 'binding_target', 'goods_price_yuan', 'is_active', 'updated_at']
    list_filter = ['is_active']
    search_fields = ['product_id', 'package__name', 'spec__name', 'spec__package__name', 'remark']
    autocomplete_fields = ['package', 'spec']
    list_editable = ['is_active']

    @admin.display(description='绑定对象')
    def binding_target(self, obj):
        return obj.spec or obj.package

    @admin.display(description='单价')
    def goods_price_yuan(self, obj):
        return f'¥{obj.goods_price_fen / 100:.2f}'


@admin.register(PaymentCallbackLog)
class PaymentCallbackLogAdmin(admin.ModelAdmin):
    list_display = ['id', 'payment_no', 'channel', 'verify_result', 'handled_result', 'created_at']
    list_filter = ['channel', 'verify_result']
