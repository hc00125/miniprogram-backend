from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ActionForm
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.wallet.diamonds import diamonds_to_yuan, yuan_to_diamonds
from apps.wallet.models import ClientWallet
from apps.wallet.services import create_manual_wallet_adjustment

from . import admin as accounts_admin  # noqa: F401  确保 ClientProfileAdmin 已注册
from .models import ClientProfile


class ClientWalletAdjustActionForm(ActionForm):
    wallet_diamonds = forms.IntegerField(
        required=False,
        label='调整老板钻石（整数，负数为扣减）',
    )
    wallet_adjust_reason = forms.CharField(
        required=False,
        label='钻石调整原因',
        max_length=500,
    )


@admin.action(description='手动调整老板钻石（无钱包自动创建）', permissions=['change'])
def adjust_client_wallets(self, request, queryset):
    raw_diamonds = (request.POST.get('wallet_diamonds') or '').strip()
    reason = (request.POST.get('wallet_adjust_reason') or '').strip()

    if not raw_diamonds or not reason:
        self.message_user(
            request,
            '手动调整老板钻石必须填写非零整数钻石与调整原因',
            level=messages.ERROR,
        )
        return

    try:
        diamonds = int(raw_diamonds)
        if str(diamonds) != raw_diamonds or diamonds == 0:
            raise ValueError
        amount_yuan = diamonds_to_yuan(abs(diamonds))
        if diamonds < 0:
            amount_yuan = -amount_yuan
    except (ValueError, DjangoValidationError, DRFValidationError):
        self.message_user(request, '调整钻石必须是非零整数', level=messages.ERROR)
        return

    success_count = 0
    created_wallet_count = 0
    for profile in queryset.select_related('user').iterator():
        wallet_existed = ClientWallet.objects.filter(profile=profile).exists()
        try:
            create_manual_wallet_adjustment(
                profile=profile,
                amount=amount_yuan,
                reason=f'{reason}（调整钻石 {diamonds:+d}）',
                operator=request.user,
            )
            success_count += 1
            if not wallet_existed:
                created_wallet_count += 1
        except (DjangoValidationError, DRFValidationError) as exc:
            detail = getattr(exc, 'detail', None) or getattr(exc, 'messages', None) or str(exc)
            self.message_user(
                request,
                f'{profile}: {detail}',
                level=messages.WARNING,
            )

    if success_count:
        self.message_user(
            request,
            f'已调整 {success_count} 位老板的钻石；其中自动创建 {created_wallet_count} 个钱包。',
            level=messages.SUCCESS,
        )


@admin.display(description='钱包钻石')
def wallet_diamonds_display(self, obj):
    try:
        wallet = obj.wallet
    except ClientWallet.DoesNotExist:
        return '未创建'
    return yuan_to_diamonds(wallet.balance)


client_profile_admin = admin.site._registry.get(ClientProfile)
if client_profile_admin:
    admin_class = client_profile_admin.__class__
    admin_class.action_form = ClientWalletAdjustActionForm
    admin_class.adjust_client_wallets = adjust_client_wallets
    admin_class.wallet_diamonds_display = wallet_diamonds_display

    actions = list(admin_class.actions)
    if 'adjust_client_wallets' not in actions:
        actions.append('adjust_client_wallets')
    admin_class.actions = actions

    list_display = list(admin_class.list_display)
    if 'wallet_diamonds_display' not in list_display:
        insert_at = list_display.index('cumulative_consumption') + 1
        list_display.insert(insert_at, 'wallet_diamonds_display')
    admin_class.list_display = list_display
