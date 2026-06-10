from django.contrib import admin

from .models import Payment, PaymentCallbackLog


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['id', 'payment_no', 'order', 'channel', 'scene', 'amount', 'status', 'created_at']
    list_filter = ['channel', 'scene', 'status']
    search_fields = ['payment_no', 'order__order_no', 'third_trade_no']


@admin.register(PaymentCallbackLog)
class PaymentCallbackLogAdmin(admin.ModelAdmin):
    list_display = ['id', 'payment_no', 'channel', 'verify_result', 'handled_result', 'created_at']
    list_filter = ['channel', 'verify_result']
