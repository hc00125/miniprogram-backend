from django import forms
from django.contrib import admin, messages
from django.db import transaction
from rest_framework.exceptions import ValidationError

from .models import (
    EarningsConfig,
    OrderCommissionOverride,
    PlayerEarning,
    PlayerWallet,
    WalletAdjustment,
    WalletLedger,
    Withdrawal,
    WithdrawalAllocation,
)
from .services import (
    apply_wallet_adjustment,
    approve_withdrawal,
    freeze_pending_earning,
    mark_withdrawal_paid,
    recalculate_pending_order_earnings,
    return_withdrawal,
    unfreeze_earning,
)


def boss_name_for_order(order):
    profile = getattr(order.boss_user, 'client_profile', None)
    nickname = (getattr(profile, 'nickname', '') or '').strip()
    return nickname or '未设置昵称'


def package_name_for_order(order):
    return order.package_name_snapshot or getattr(order.package, 'name', '') or '陪玩服务'


@admin.register(EarningsConfig)
class EarningsConfigAdmin(admin.ModelAdmin):
    list_display = ['key', 'default_commission_rate', 'review_days', 'min_withdrawal_amount', 'updated_by', 'updated_at']
    fields = ['key', 'default_commission_rate', 'review_days', 'min_withdrawal_amount', 'updated_by', 'updated_at']
    readonly_fields = ['updated_by', 'updated_at']

    def has_add_permission(self, request):
        return not EarningsConfig.objects.exists()

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(OrderCommissionOverride)
class OrderCommissionOverrideAdmin(admin.ModelAdmin):
    list_display = ['order', 'commission_rate', 'reason', 'created_by', 'updated_at']
    search_fields = ['order__order_no', 'reason']
    readonly_fields = ['created_by', 'created_at', 'updated_at']
    autocomplete_fields = ['order']

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            if not obj.created_by_id:
                obj.created_by = request.user
            super().save_model(request, obj, form, change)
            recalculate_pending_order_earnings(obj.order, operator=request.user)


@admin.register(PlayerWallet)
class PlayerWalletAdmin(admin.ModelAdmin):
    list_display = [
        'player', 'pending_balance', 'available_balance', 'withdrawing_balance',
        'withdrawn_total', 'debt_balance', 'updated_at',
    ]
    search_fields = ['player__name', 'player__user__username']
    readonly_fields = [
        'player', 'pending_balance', 'available_balance', 'withdrawing_balance',
        'withdrawn_total', 'debt_balance', 'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PlayerEarning)
class PlayerEarningAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'order', 'boss_name', 'player', 'package_name', 'order_total',
        'gross_amount', 'commission_rate', 'commission_amount', 'net_amount',
        'reversed_amount', 'debt_offset_amount', 'status', 'review_until',
        'available_amount', 'withdrawing_amount', 'withdrawn_amount',
    ]
    list_filter = ['status', 'commission_rate']
    search_fields = [
        'order__order_no', 'order__boss_user__client_profile__nickname',
        'player__name', 'order__package_name_snapshot', 'order__package__name',
    ]
    list_select_related = ['order__boss_user__client_profile', 'order__package', 'player']
    readonly_fields = [
        'order', 'boss_name', 'player', 'package_name', 'order_total',
        'gross_amount', 'commission_rate', 'commission_amount',
        'net_amount', 'reversed_amount', 'debt_offset_amount', 'review_until',
        'available_at', 'available_amount', 'withdrawing_amount', 'withdrawn_amount',
        'created_at', 'updated_at',
    ]
    actions = ['freeze_selected', 'unfreeze_selected']

    @admin.display(description='老板', ordering='order__boss_user__client_profile__nickname')
    def boss_name(self, obj):
        return boss_name_for_order(obj.order)

    @admin.display(description='套餐', ordering='order__package_name_snapshot')
    def package_name(self, obj):
        return package_name_for_order(obj.order)

    @admin.display(description='订单金额', ordering='order__total_amount')
    def order_total(self, obj):
        return obj.order.total_amount

    @admin.action(description='冻结选中的审核中工资')
    def freeze_selected(self, request, queryset):
        success_count = 0
        for earning in queryset:
            try:
                freeze_pending_earning(earning, '管理员在后台冻结')
                success_count += 1
            except ValidationError as exc:
                self.message_user(request, f'{earning}: {exc.detail}', level=messages.WARNING)
        if success_count:
            self.message_user(request, f'已冻结 {success_count} 条工资记录', level=messages.SUCCESS)

    @admin.action(description='解除选中工资的冻结')
    def unfreeze_selected(self, request, queryset):
        success_count = 0
        for earning in queryset:
            try:
                unfreeze_earning(earning)
                success_count += 1
            except ValidationError as exc:
                self.message_user(request, f'{earning}: {exc.detail}', level=messages.WARNING)
        if success_count:
            self.message_user(request, f'已解除 {success_count} 条工资冻结', level=messages.SUCCESS)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class WalletAdjustmentAdminForm(forms.ModelForm):
    class Meta:
        model = WalletAdjustment
        fields = '__all__'

    def clean_adjustment_type(self):
        value = self.cleaned_data['adjustment_type']
        if not self.instance.pk and value == WalletAdjustment.TYPE_REFUND_REVERSAL:
            raise forms.ValidationError('退款工资冲销由退款记录自动生成，不能手动创建。')
        return value

    def clean_reason(self):
        value = (self.cleaned_data.get('reason') or '').strip()
        if not value:
            raise forms.ValidationError('必须填写奖惩原因。')
        return value


@admin.register(WalletAdjustment)
class WalletAdjustmentAdmin(admin.ModelAdmin):
    form = WalletAdjustmentAdminForm
    list_display = [
        'id', 'player', 'adjustment_type', 'amount', 'available_delta',
        'pending_delta', 'debt_delta', 'order', 'created_by', 'applied_at',
    ]
    list_filter = ['adjustment_type', 'applied_at', 'created_at']
    search_fields = ['player__name', 'order__order_no', 'reason', 'reference_id']
    autocomplete_fields = ['player', 'order']
    readonly_fields = [
        'available_delta', 'pending_delta', 'debt_delta', 'reference_type',
        'reference_id', 'created_by', 'applied_at', 'created_at',
    ]
    fields = [
        'player', 'order', 'adjustment_type', 'amount', 'reason', 'evidence_url',
        'available_delta', 'pending_delta', 'debt_delta',
        'reference_type', 'reference_id', 'created_by', 'applied_at', 'created_at',
    ]

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj and obj.applied_at:
            fields.extend(['player', 'order', 'adjustment_type', 'amount', 'reason', 'evidence_url'])
        return fields

    def save_model(self, request, obj, form, change):
        if change:
            super().save_model(request, obj, form, change)
            return
        with transaction.atomic():
            obj.created_by = request.user
            super().save_model(request, obj, form, change)
            apply_wallet_adjustment(obj, operator=request.user)

    def has_delete_permission(self, request, obj=None):
        return False


class WithdrawalAllocationInline(admin.TabularInline):
    model = WithdrawalAllocation
    extra = 0
    can_delete = False
    fields = ['earning', 'order_no', 'boss_name', 'package_name', 'amount', 'created_at']
    readonly_fields = ['earning', 'order_no', 'boss_name', 'package_name', 'amount', 'created_at']

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'earning__order__boss_user__client_profile',
            'earning__order__package',
        )

    @admin.display(description='订单号')
    def order_no(self, obj):
        return obj.earning.order.order_no

    @admin.display(description='老板')
    def boss_name(self, obj):
        return boss_name_for_order(obj.earning.order)

    @admin.display(description='套餐')
    def package_name(self, obj):
        return package_name_for_order(obj.earning.order)


@admin.register(Withdrawal)
class WithdrawalAdmin(admin.ModelAdmin):
    list_display = [
        'withdrawal_no', 'player', 'amount', 'status', 'payment_method',
        'account_name', 'transfer_no', 'created_at', 'paid_at',
    ]
    list_filter = ['status', 'payment_method', 'created_at']
    search_fields = [
        'withdrawal_no', 'player__name', 'account_name', 'account_no', 'transfer_no',
        'allocations__earning__order__order_no',
        'allocations__earning__order__boss_user__client_profile__nickname',
    ]
    readonly_fields = [
        'withdrawal_no', 'player', 'wallet', 'amount', 'status', 'payment_method',
        'account_name', 'account_no', 'request_note', 'reviewed_by', 'reviewed_at',
        'paid_by', 'paid_at', 'created_at', 'updated_at',
    ]
    fields = [
        'withdrawal_no', 'player', 'wallet', 'amount', 'status', 'payment_method',
        'account_name', 'account_no', 'request_note', 'transfer_no', 'proof_url',
        'admin_note', 'reject_reason', 'reviewed_by', 'reviewed_at', 'paid_by',
        'paid_at', 'created_at', 'updated_at',
    ]
    inlines = [WithdrawalAllocationInline]
    actions = ['approve_selected', 'mark_paid_selected', 'reject_selected', 'mark_failed_selected']

    @admin.action(description='审核通过，进入待打款')
    def approve_selected(self, request, queryset):
        success_count = 0
        for withdrawal in queryset:
            try:
                approve_withdrawal(withdrawal, request.user)
                success_count += 1
            except ValidationError as exc:
                self.message_user(request, f'{withdrawal.withdrawal_no}: {exc.detail}', level=messages.WARNING)
        if success_count:
            self.message_user(request, f'已通过 {success_count} 笔提现审核', level=messages.SUCCESS)

    @admin.action(description='确认财务已手动打款')
    def mark_paid_selected(self, request, queryset):
        success_count = 0
        for withdrawal in queryset:
            try:
                mark_withdrawal_paid(withdrawal, request.user)
                success_count += 1
            except ValidationError as exc:
                self.message_user(request, f'{withdrawal.withdrawal_no}: {exc.detail}', level=messages.WARNING)
        if success_count:
            self.message_user(request, f'已确认 {success_count} 笔提现完成', level=messages.SUCCESS)

    @admin.action(description='驳回提现并退回可提现鱼干')
    def reject_selected(self, request, queryset):
        success_count = 0
        for withdrawal in queryset:
            try:
                reason = withdrawal.reject_reason or withdrawal.admin_note or '财务审核未通过'
                return_withdrawal(withdrawal, reason, request.user)
                success_count += 1
            except ValidationError as exc:
                self.message_user(request, f'{withdrawal.withdrawal_no}: {exc.detail}', level=messages.WARNING)
        if success_count:
            self.message_user(request, f'已驳回 {success_count} 笔提现并退回余额', level=messages.SUCCESS)

    @admin.action(description='标记打款失败并退回可提现鱼干')
    def mark_failed_selected(self, request, queryset):
        success_count = 0
        for withdrawal in queryset:
            try:
                reason = withdrawal.reject_reason or withdrawal.admin_note or '财务打款失败'
                return_withdrawal(withdrawal, reason, request.user, failed=True)
                success_count += 1
            except ValidationError as exc:
                self.message_user(request, f'{withdrawal.withdrawal_no}: {exc.detail}', level=messages.WARNING)
        if success_count:
            self.message_user(request, f'已退回 {success_count} 笔失败提现', level=messages.SUCCESS)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WalletLedger)
class WalletLedgerAdmin(admin.ModelAdmin):
    list_display = ['id', 'wallet', 'entry_type', 'bucket', 'delta', 'balance_after', 'reference_id', 'created_at']
    list_filter = ['entry_type', 'bucket', 'created_at']
    search_fields = ['wallet__player__name', 'reference_id', 'note']
    readonly_fields = [
        'wallet', 'entry_type', 'bucket', 'delta', 'balance_after', 'reference_type',
        'reference_id', 'note', 'operator', 'created_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in {'GET', 'HEAD', 'OPTIONS'}

    def has_delete_permission(self, request, obj=None):
        return False
