from decimal import Decimal

from django.contrib import admin
from django.db.models import Sum

from apps.payments import admin as payments_admin
from apps.payments.models import Refund

from .models import RefundPayment, RefundRecord


# 支付管理只负责查询支付事实；退款动作和退款记录统一移动到“退款管理”。
payments_admin.PaymentAdmin.actions = []
payments_admin.PaymentAdmin.list_display = [
    'id', 'payment_no', 'boss_user_id', 'order', 'channel', 'scene',
    'amount', 'status', 'created_at',
]

try:
    admin.site.unregister(Refund)
except admin.sites.NotRegistered:
    pass


@admin.register(RefundPayment)
class RefundPaymentAdmin(payments_admin.PaymentAdmin):
    """客服/财务统一退款工作台。"""

    list_display = [
        'id', 'payment_no', 'boss_user_id', 'order', 'channel',
        'amount', 'status', 'diamond_refund_state',
        'wechat_original_refund_state', 'created_at',
    ]
    list_filter = ['channel', 'status', 'created_at']
    actions = [
        'refund_to_diamonds',
        'submit_wechat_original_refund',
        'sync_wechat_original_refund_status',
    ]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(status__in=['paid', 'refunded'])
            .prefetch_related('refunds')
        )

    @admin.display(description='钻石退款')
    def diamond_refund_state(self, obj):
        total = obj.refunds.filter(status=Refund.STATUS_SUCCEEDED).aggregate(
            total=Sum('amount')
        )['total'] or Decimal('0.00')
        if not total:
            return '未退款'
        if Decimal(str(total)) >= Decimal(str(obj.amount)):
            return f'已退 ¥{Decimal(str(total)):.2f} 等值钻石'
        return f'部分退款 ¥{Decimal(str(total)):.2f} 等值钻石'

    @admin.action(description='① 普通退款：退回用户钻石钱包')
    def refund_to_diamonds(self, request, queryset):
        return payments_admin.PaymentAdmin.create_full_refunds(self, request, queryset)

    @admin.action(description='② 微信虚拟支付：发起微信原路退款')
    def submit_wechat_original_refund(self, request, queryset):
        return payments_admin.PaymentAdmin.refund_wechat_virtual_original(self, request, queryset)

    @admin.action(description='③ 微信虚拟支付：同步原路退款状态')
    def sync_wechat_original_refund_status(self, request, queryset):
        return payments_admin.PaymentAdmin.sync_wechat_virtual_original_refunds(
            self,
            request,
            queryset,
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RefundRecord)
class RefundRecordAdmin(payments_admin.RefundAdmin):
    """沿用原有退款审计能力，但在独立菜单中展示。"""

    pass
