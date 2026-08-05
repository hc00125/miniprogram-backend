from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ActionForm
from django.contrib.admin.widgets import AutocompleteSelect
from django.core.exceptions import PermissionDenied, ValidationError as DjangoValidationError
from django.template.response import TemplateResponse
from django.urls import path, reverse
from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.accounts.models import ClientProfile

from .diamonds import diamonds_to_yuan, yuan_to_diamonds
from .models import ClientWallet, ClientWalletLedger, RechargeOrder, RechargeProduct
from .services import create_manual_wallet_adjustment


class WalletAdjustActionForm(ActionForm):
    diamonds = forms.IntegerField(
        required=False,
        label='调整钻石（整数，负数为扣减）',
    )
    reason = forms.CharField(required=False, label='调整原因')


class ManualWalletAdjustmentForm(forms.Form):
    profile = forms.ModelChoiceField(
        queryset=ClientProfile.objects.none(),
        label='老板用户',
        help_text='可搜索昵称、OpenID或后台用户名。没有钱包的用户会自动创建钱包。',
    )
    diamonds = forms.IntegerField(
        label='调整钻石',
        help_text='填写正整数增加余额，填写负整数扣减余额。',
    )
    reason = forms.CharField(
        label='调整原因',
        max_length=500,
        widget=forms.Textarea(attrs={'rows': 4}),
        help_text='必填，将写入老板钱包流水并记录操作管理员。',
    )

    def __init__(self, *args, admin_site=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['profile'].queryset = (
            ClientProfile.objects.select_related('user').order_by('-created_at', '-id')
        )
        relation_field = ClientWallet._meta.get_field('profile')
        self.fields['profile'].widget = AutocompleteSelect(
            relation_field,
            admin_site or admin.site,
            attrs={'style': 'width: 36em;'},
        )

    def clean_diamonds(self):
        value = self.cleaned_data['diamonds']
        if value == 0:
            raise forms.ValidationError('调整钻石不能为0')
        try:
            diamonds_to_yuan(abs(value))
        except (DjangoValidationError, DRFValidationError) as exc:
            raise forms.ValidationError('调整钻石必须是有效的非零整数') from exc
        return value

    def clean_reason(self):
        value = (self.cleaned_data.get('reason') or '').strip()
        if not value:
            raise forms.ValidationError('必须填写调整原因')
        return value


@admin.register(ClientWallet)
class ClientWalletAdmin(admin.ModelAdmin):
    action_form = WalletAdjustActionForm
    change_list_template = 'admin/wallet/clientwallet/change_list.html'
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

    def changelist_view(self, request, extra_context=None):
        extra_context = {
            **(extra_context or {}),
            'can_manual_adjust_wallet': self.has_change_permission(request),
        }
        return super().changelist_view(request, extra_context=extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'manual-adjust/',
                self.admin_site.admin_view(self.manual_adjust_view),
                name='wallet_clientwallet_manual_adjust',
            ),
        ]
        return custom_urls + urls

    def manual_adjust_view(self, request):
        if not self.has_change_permission(request):
            raise PermissionDenied

        form = ManualWalletAdjustmentForm(
            request.POST or None,
            admin_site=self.admin_site,
        )
        if request.method == 'POST' and form.is_valid():
            profile = form.cleaned_data['profile']
            diamond_value = form.cleaned_data['diamonds']
            reason = form.cleaned_data['reason']
            amount_yuan = diamonds_to_yuan(abs(diamond_value))
            if diamond_value < 0:
                amount_yuan = -amount_yuan

            existing_wallet = ClientWallet.objects.filter(profile=profile).first()
            before_diamonds = yuan_to_diamonds(existing_wallet.balance) if existing_wallet else 0
            try:
                entry = create_manual_wallet_adjustment(
                    profile=profile,
                    amount=amount_yuan,
                    reason=f'{reason}（调整钻石 {diamond_value:+d}）',
                    operator=request.user,
                )
            except (DjangoValidationError, DRFValidationError) as exc:
                detail = getattr(exc, 'detail', None) or getattr(exc, 'messages', None) or str(exc)
                form.add_error(None, detail)
            else:
                after_diamonds = yuan_to_diamonds(entry.balance_after)
                wallet_note = '，并已自动创建钱包' if existing_wallet is None else ''
                self.message_user(
                    request,
                    f'已为“{profile}”调整 {diamond_value:+d} 钻石{wallet_note}。'
                    f'余额：{before_diamonds} → {after_diamonds} 钻石。',
                    level=messages.SUCCESS,
                )
                from django.shortcuts import redirect

                return redirect(reverse('admin:wallet_clientwallet_changelist'))

        context = {
            **self.admin_site.each_context(request),
            'title': '手动调整用户钻石',
            'opts': self.model._meta,
            'form': form,
            'media': self.media + form.media,
            'changelist_url': reverse('admin:wallet_clientwallet_changelist'),
        }
        request.current_app = self.admin_site.name
        return TemplateResponse(
            request,
            'admin/wallet/clientwallet/manual_adjust.html',
            context,
        )

    # 现有钱包仍支持在列表中批量调整；未创建钱包的用户使用右上角
    # “手动调整用户钻石”入口，系统会自动创建钱包。
    @admin.action(description='批量调整已存在钱包（需在上方填写钻石与原因）', permissions=['change'])
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
        except (ValueError, DjangoValidationError, DRFValidationError):
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
            except (DjangoValidationError, DRFValidationError) as exc:
                detail = getattr(exc, 'detail', None) or getattr(exc, 'messages', None) or str(exc)
                self.message_user(request, f'{wallet}: {detail}', level=messages.WARNING)
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
