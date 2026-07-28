from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ActionForm
from rest_framework.exceptions import ValidationError

from .diamonds import diamonds_to_yuan, yuan_to_diamonds
from .models import ClientWallet, ClientWalletLedger, RechargeOrder, RechargeProduct
from .services import create_manual_wallet_adjustment


class WalletAdjustActionForm(ActionForm):
    diamonds = forms.IntegerField(
        required=False,
        label='调整钻石（整数，负数为扣减）',
    )
    reason = forms.CharField(required=False, label='调整原因')


@admin.register(ClientWallet)
class ClientWalletAdmin(admin.ModelAdmin):
    action_form = WalletAdjustActionForm
    list_display = [
        'id', 'profile', 'available_diamonds', 'balance',
        'recharged_diamonds', 'spent_diamonds', 'updated_at',
    ]
    search_fields = ['profile__nickname', 'profile__openid', 'profile__user__username']
    readonly_fields = ['profile', 'balance', 'recharged_total', 'spent_total', 'created_at', 'updated_at']
    actions = ['manual_adjust']

    @admin.display(description='可用钻石', ordering='balance')
    def available_diamonds(self, obj):
        return yuan_to_diamonds(obj.balance)

    @admin.display(description='累计充值钻石', ordering='recharged_total')
    def recharged_diamonds(self, obj):
        return yuan_to_diamonds(obj.recharged_total)

    @admin.display(description='累计支付钻石', ordering='spent_total')
    def spent_diamonds(self, obj):
        return yuan_to_diamonds(obj.spent_total)

    # permissions=['change']：仅持有 change_clientwallet 权限的管理员可执行，
    # 只读（view）权限的客服人员不能凭此动作增减余额。
    @admin.action(description='手工调整可用钻石（需在上方填写整数钻石与原因）', permissions=['change'])
    def manual_adjust(self, request, queryset):
        diamonds = (request.POST.get('diamonds') or '').strip()
        reason = (request.POST.get('reason') or '').strip()
        if not diamonds or not reason:
            self.message_user(request, '手工调整必须填写整数钻石与原因', level=messages.ERROR)
            return
        try:
            diamond_value = int(diamonds)
            if str(diamond_value) != diamonds or diamond_value == 0:
                raise ValueError
            amount_yuan = diamonds_to_yuan(abs(diamond_value))
            if diamond_value < 0:
                amount_yuan = -amount_yuan
        except (ValueError, ValidationError):
            self.message_user(request, '调整钻石必须是非零整数', level=messages.ERROR)
            return

        success_count = 0
        for wallet in queryset.select_related('profile'):
            try:
                create_manual_wallet_adjustment(
                    profile=wallet.profile,
                    amount=amount_yuan,
                    reason=f'{reason}（调整钻石 {diamond_value:+d}）',
                    operator=request.user,
                )
                success_count += 1
            except ValidationError as exc:
                self.message_user(request, f'{wallet}: {exc.detail}', level=messages.WARNING)
        if success_count:
            self.message_user(request, f'已完成 {success_count} 个钱包的钻石调整', level=messages.SUCCESS)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RechargeProduct)
class RechargeProductAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'diamond_amount_display', 'amount', 'product_id', 'goods_price_fen',
        'is_active', 'sort_order', 'remark', 'updated_at',
    ]
    list_filter = ['is_active']
    search_fields = ['product_id', 'remark']
    list_editable = ['is_active', 'sort_order']

    @admin.display(description='到账钻石', ordering='amount')
    def diamond_amount_display(self, obj):
        return obj.diamond_amount


@admin.register(RechargeOrder)
class RechargeOrderAdmin(admin.ModelAdmin):
    list_display = [
        'recharge_no', 'profile', 'diamond_amount_display', 'amount', 'product',
        'channel', 'status', 'third_trade_no', 'paid_at', 'credited_at', 'created_at',
    ]
    list_filter = ['status', 'channel', 'created_at']
    search_fields = ['recharge_no', 'third_trade_no', 'profile__nickname', 'profile__openid']
    readonly_fields = [
        'recharge_no', 'profile', 'product', 'amount', 'channel', 'status',
        'third_trade_no', 'notify_payload', 'paid_at', 'credited_at',
        'expires_at', 'created_at', 'updated_at',
    ]

    @admin.display(description='到账钻石', ordering='amount')
    def diamond_amount_display(self, obj):
        return obj.diamond_amount

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in {'GET', 'HEAD', 'OPTIONS'}

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ClientWalletLedger)
class ClientWalletLedgerAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'wallet', 'entry_type', 'amount_diamonds_display', 'amount',
        'balance_after_diamonds_display', 'balance_after', 'reference_type',
        'reference_id', 'operator', 'created_at',
    ]
    list_filter = ['entry_type', 'created_at']
    search_fields = ['wallet__profile__nickname', 'wallet__profile__openid', 'reference_id', 'note']
    readonly_fields = [
        'wallet', 'entry_type', 'amount', 'balance_after', 'reference_type',
        'reference_id', 'operator', 'note', 'created_at',
    ]

    @admin.display(description='变动钻石', ordering='amount')
    def amount_diamonds_display(self, obj):
        return obj.amount_diamonds

    @admin.display(description='变动后钻石', ordering='balance_after')
    def balance_after_diamonds_display(self, obj):
        return obj.balance_after_diamonds

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in {'GET', 'HEAD', 'OPTIONS'}

    def has_delete_permission(self, request, obj=None):
        return False
