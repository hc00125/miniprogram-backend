from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ActionForm
from rest_framework.exceptions import ValidationError

from .models import ClientWallet, ClientWalletLedger, RechargeOrder, RechargeProduct
from .services import create_manual_wallet_adjustment


class WalletAdjustActionForm(ActionForm):
    amount = forms.DecimalField(
        required=False,
        max_digits=12,
        decimal_places=2,
        label='调整金额(元，负数为扣减)',
    )
    reason = forms.CharField(required=False, label='调整原因')


@admin.register(ClientWallet)
class ClientWalletAdmin(admin.ModelAdmin):
    action_form = WalletAdjustActionForm
    list_display = ['id', 'profile', 'balance', 'recharged_total', 'spent_total', 'updated_at']
    search_fields = ['profile__nickname', 'profile__openid', 'profile__user__username']
    readonly_fields = ['profile', 'balance', 'recharged_total', 'spent_total', 'created_at', 'updated_at']
    actions = ['manual_adjust']

    # permissions=['change']：仅持有 change_clientwallet 权限的管理员可执行，
    # 只读（view）权限的客服人员不能凭此动作增减余额。
    @admin.action(description='手工调整余额（需在上方填写金额与原因）', permissions=['change'])
    def manual_adjust(self, request, queryset):
        amount = (request.POST.get('amount') or '').strip()
        reason = (request.POST.get('reason') or '').strip()
        if not amount or not reason:
            self.message_user(request, '手工调整必须填写金额与原因', level=messages.ERROR)
            return
        success_count = 0
        for wallet in queryset.select_related('profile'):
            try:
                create_manual_wallet_adjustment(
                    profile=wallet.profile,
                    amount=amount,
                    reason=reason,
                    operator=request.user,
                )
                success_count += 1
            except ValidationError as exc:
                self.message_user(request, f'{wallet}: {exc.detail}', level=messages.WARNING)
        if success_count:
            self.message_user(request, f'已完成 {success_count} 个钱包的手工调整', level=messages.SUCCESS)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RechargeProduct)
class RechargeProductAdmin(admin.ModelAdmin):
    list_display = ['id', 'amount', 'product_id', 'goods_price_fen', 'is_active', 'sort_order', 'remark', 'updated_at']
    list_filter = ['is_active']
    search_fields = ['product_id', 'remark']
    list_editable = ['is_active', 'sort_order']


@admin.register(RechargeOrder)
class RechargeOrderAdmin(admin.ModelAdmin):
    list_display = [
        'recharge_no', 'profile', 'product', 'amount', 'channel', 'status',
        'third_trade_no', 'paid_at', 'credited_at', 'created_at',
    ]
    list_filter = ['status', 'channel', 'created_at']
    search_fields = ['recharge_no', 'third_trade_no', 'profile__nickname', 'profile__openid']
    readonly_fields = [
        'recharge_no', 'profile', 'product', 'amount', 'channel', 'status',
        'third_trade_no', 'notify_payload', 'paid_at', 'credited_at',
        'expires_at', 'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in {'GET', 'HEAD', 'OPTIONS'}

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ClientWalletLedger)
class ClientWalletLedgerAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'wallet', 'entry_type', 'amount', 'balance_after',
        'reference_type', 'reference_id', 'operator', 'created_at',
    ]
    list_filter = ['entry_type', 'created_at']
    search_fields = ['wallet__profile__nickname', 'wallet__profile__openid', 'reference_id', 'note']
    readonly_fields = [
        'wallet', 'entry_type', 'amount', 'balance_after', 'reference_type',
        'reference_id', 'operator', 'note', 'created_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in {'GET', 'HEAD', 'OPTIONS'}

    def has_delete_permission(self, request, obj=None):
        return False
