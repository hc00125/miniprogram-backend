from django.contrib import admin
from django.utils import timezone

from .payment_window_models import OrderPaymentWindow


@admin.register(OrderPaymentWindow)
class OrderPaymentWindowAdmin(admin.ModelAdmin):
    list_display = [
        'order',
        'started_at',
        'deadline_at',
        'confirmation_deadline_at',
        'current_phase',
        'expired_at',
        'expire_reason',
    ]
    list_filter = ['expired_at', 'started_at', 'deadline_at']
    search_fields = [
        'order__order_no',
        'order__boss_wechat',
        'order__package_name_snapshot',
        'expire_reason',
    ]
    readonly_fields = [
        'order',
        'started_at',
        'deadline_at',
        'confirmation_deadline_at',
        'expired_at',
        'expire_reason',
        'created_at',
        'updated_at',
    ]
    ordering = ['-started_at']
    list_select_related = ['order']

    @admin.display(description='当前阶段')
    def current_phase(self, obj):
        if obj.order.paid:
            return '已付款'
        if obj.expired_at:
            return '已超时取消'
        now = timezone.now()
        if now < obj.deadline_at:
            return '支付窗口开放'
        if now < obj.confirmation_deadline_at:
            return '微信核验中'
        return '等待自动处理'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
