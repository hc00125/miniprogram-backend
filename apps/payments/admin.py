from decimal import Decimal

from django.contrib import admin, messages
from django.db import transaction
from django.db.models import Sum
from rest_framework.exceptions import ValidationError

from apps.earnings.services import reverse_refund_earnings

from . import services as payment_services
from .models import Payment, PaymentCallbackLog, Refund, VirtualProductBinding
from .refund_integrity import settle_balance_refund


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['id', 'payment_no', 'order', 'channel', 'scene', 'amount', 'status', 'created_at']
    list_filter = ['channel', 'scene', 'status']
    search_fields = ['payment_no', 'order__order_no', 'third_trade_no']
    actions = ['create_full_refunds']

    @admin.action(description='为选中的已支付记录创建剩余金额退款')
    def create_full_refunds(self, request, queryset):
        balance_succeeded = 0
        external_pending = 0
        for payment in queryset.select_related('order'):
            if payment.status != 'paid':
                self.message_user(request, f'{payment.payment_no} 不是已支付状态，已跳过', level=messages.WARNING)
                continue
            active_total = payment.refunds.filter(
                status__in=[Refund.STATUS_PENDING, Refund.STATUS_PROCESSING, Refund.STATUS_SUCCEEDED]
            ).aggregate(total=Sum('amount'))['total'] or 0
            remaining = Decimal(str(payment.amount)) - Decimal(str(active_total))
            if remaining <= 0:
                self.message_user(request, f'{payment.payment_no} 已无可退金额', level=messages.WARNING)
                continue
            try:
                refund = payment_services.create_refund(
                    payment.payment_no,
                    remaining,
                    reason='管理员后台发起退款',
                    operator=request.user,
                )
                if refund.status == Refund.STATUS_SUCCEEDED and payment.channel == 'balance':
                    balance_succeeded += 1
                else:
                    external_pending += 1
            except ValidationError as exc:
                self.message_user(request, f'{payment.payment_no}: {exc.detail}', level=messages.WARNING)
        if balance_succeeded:
            self.message_user(
                request,
                f'{balance_succeeded} 笔余额退款已立即退回老板钱包',
                level=messages.SUCCESS,
            )
        if external_pending:
            self.message_user(
                request,
                f'已创建 {external_pending} 笔第三方支付待处理退款',
                level=messages.SUCCESS,
            )


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = [
        'refund_no', 'payment', 'order', 'amount', 'status',
        'third_refund_no', 'created_by', 'created_at',
    ]
    list_filter = ['status', 'payment__channel', 'created_at']
    search_fields = ['refund_no', 'payment__payment_no', 'order__order_no', 'third_refund_no', 'reason']
    readonly_fields = [
        'refund_no', 'payment', 'order', 'amount', 'reason', 'status',
        'third_refund_no', 'notify_payload', 'created_by', 'created_at', 'updated_at',
    ]
    fields = [
        'refund_no', 'payment', 'order', 'amount', 'reason', 'status',
        'third_refund_no', 'failed_reason', 'notify_payload',
        'created_by', 'created_at', 'updated_at',
    ]
    actions = ['confirm_refund_succeeded', 'mark_refund_failed']

    @admin.action(description='确认退款成功并自动冲销陪玩工资')
    def confirm_refund_succeeded(self, request, queryset):
        success_count = 0
        for refund in queryset:
            try:
                if refund.payment.channel == 'balance':
                    settled = settle_balance_refund(refund.pk, operator=request.user)
                    if settled.status == Refund.STATUS_SUCCEEDED:
                        success_count += 1
                    continue

                with transaction.atomic():
                    locked = Refund.objects.select_for_update().select_related('payment', 'order').get(pk=refund.pk)
                    if locked.status == Refund.STATUS_SUCCEEDED:
                        reverse_refund_earnings(locked, operator=request.user)
                        continue
                    if locked.status not in {Refund.STATUS_PENDING, Refund.STATUS_PROCESSING}:
                        raise ValidationError({'detail': '只有待处理或退款中的记录可以确认成功'})
                    locked.status = Refund.STATUS_SUCCEEDED
                    locked.failed_reason = ''
                    locked.save(update_fields=['status', 'failed_reason', 'updated_at'])
                    reverse_refund_earnings(locked, operator=request.user)

                    succeeded_total = locked.payment.refunds.filter(
                        status=Refund.STATUS_SUCCEEDED
                    ).aggregate(total=Sum('amount'))['total'] or 0
                    if Decimal(str(succeeded_total)) >= Decimal(str(locked.payment.amount)):
                        locked.payment.status = 'refunded'
                        locked.payment.save(update_fields=['status', 'updated_at'])
                    success_count += 1
            except ValidationError as exc:
                self.message_user(request, f'{refund.refund_no}: {exc.detail}', level=messages.WARNING)
        if success_count:
            self.message_user(request, f'已确认 {success_count} 笔退款成功，并完成相关账务处理', level=messages.SUCCESS)

    @admin.action(description='标记退款失败')
    def mark_refund_failed(self, request, queryset):
        updated = queryset.filter(
            status__in=[Refund.STATUS_PENDING, Refund.STATUS_PROCESSING],
        ).exclude(payment__channel='balance').update(
            status=Refund.STATUS_FAILED,
            failed_reason='管理员标记退款失败',
        )
        self.message_user(
            request,
            f'已标记 {updated} 笔第三方退款失败；余额退款请使用对账命令重试，不能手动标记失败。',
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
