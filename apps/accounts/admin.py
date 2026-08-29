from datetime import datetime

from django import forms
from django.contrib import admin, messages
from django.db.models import Count, Q, Sum
from django.shortcuts import render
from django.urls import path
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.orders.models import Order
from apps.players.models import Player

from .models import (
    AccountRestrictionLog,
    BossConsumptionLedger,
    ClientProfile,
    ClientVipKookRoom,
    VipTier,
)
from .vip import create_manual_consumption_adjustment


class BossConsumptionLedgerAdminForm(forms.ModelForm):
    class Meta:
        model = BossConsumptionLedger
        fields = ['profile', 'amount', 'order', 'reason']
        help_texts = {
            'amount': '正数增加累计消费，负数扣减累计消费；单位为人民币元。',
            'reason': '必填。请写明线下补录、异常修正或其他调整依据。',
        }

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount == 0:
            raise forms.ValidationError('消费调整不能为0')
        return amount

    def clean_reason(self):
        reason = (self.cleaned_data.get('reason') or '').strip()
        if not reason:
            raise forms.ValidationError('人工调整必须填写原因')
        return reason


class ClientProfileAdminForm(forms.ModelForm):
    class Meta:
        model = ClientProfile
        fields = '__all__'

    def clean(self):
        cleaned = super().clean()
        account_status = cleaned.get('account_status')
        suspended_until = cleaned.get('account_suspended_until')
        reason = (cleaned.get('account_restriction_reason') or '').strip()

        if account_status == ClientProfile.ACCOUNT_STATUS_SUSPENDED:
            if not suspended_until:
                self.add_error('account_suspended_until', '暂停账户必须填写截止时间')
            elif suspended_until <= timezone.now():
                self.add_error('account_suspended_until', '暂停截止时间必须晚于当前时间')
            if not reason:
                self.add_error('account_restriction_reason', '暂停账户必须填写原因')
        elif account_status == ClientProfile.ACCOUNT_STATUS_BANNED:
            cleaned['account_suspended_until'] = None
            if not reason:
                self.add_error('account_restriction_reason', '永久封禁必须填写原因')
        elif account_status == ClientProfile.ACCOUNT_STATUS_ACTIVE:
            cleaned['account_suspended_until'] = None
            cleaned['account_restriction_reason'] = ''
        return cleaned


@admin.register(VipTier)
class VipTierAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'code', 'min_consumption', 'member_count',
        'feature_codes_display', 'badge_color', 'sort_order', 'is_active',
    ]
    list_editable = ['min_consumption', 'sort_order', 'is_active']
    list_filter = ['is_active', 'badge_color']
    search_fields = ['name', 'code']
    ordering = ['min_consumption', 'sort_order', 'id']

    @admin.display(description='会员数')
    def member_count(self, obj):
        return obj.members.count()

    @admin.display(description='可执行权益')
    def feature_codes_display(self, obj):
        return '、'.join(obj.feature_codes or []) or '-'


@admin.register(BossConsumptionLedger)
class BossConsumptionLedgerAdmin(admin.ModelAdmin):
    form = BossConsumptionLedgerAdminForm
    list_display = [
        'id', 'profile', 'source_type', 'signed_amount', 'balance_after',
        'order', 'reference_id', 'operator', 'created_at',
    ]
    list_filter = ['source_type', 'created_at']
    search_fields = [
        'profile__nickname', 'profile__openid', 'order__order_no',
        'reference_id', 'reason', 'operator__username',
    ]
    autocomplete_fields = ['profile', 'order']
    ordering = ['-created_at', '-id']
    list_select_related = ['profile', 'order', 'operator']
    date_hierarchy = 'created_at'

    @admin.display(description='变动')
    def signed_amount(self, obj):
        return f'{"+" if obj.amount > 0 else ""}{obj.amount}元'

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return [
                'profile', 'amount', 'balance_after', 'source_type', 'order',
                'reference_id', 'reason', 'operator', 'created_at',
            ]
        return []

    def save_model(self, request, obj, form, change):
        if change:
            return
        try:
            entry = create_manual_consumption_adjustment(
                profile=form.cleaned_data['profile'],
                amount=form.cleaned_data['amount'],
                reason=form.cleaned_data['reason'],
                order=form.cleaned_data.get('order'),
                operator=request.user,
            )
        except ValidationError as exc:
            raise forms.ValidationError(exc.detail) from exc
        if entry is None:
            raise forms.ValidationError('本次调整未产生有效消费变动')
        obj.pk = entry.pk
        obj.id = entry.id
        obj._state.adding = False
        self.message_user(request, '消费流水已写入并自动刷新VIP等级', level=messages.SUCCESS)

    def has_delete_permission(self, request, obj=None):
        return False


class ClientVipKookRoomInline(admin.StackedInline):
    model = ClientVipKookRoom
    extra = 0
    max_num = 1
    can_delete = False
    fields = [
        'kook_room_number', 'is_active',
        'assigned_by', 'assigned_at', 'created_at', 'updated_at',
    ]
    readonly_fields = ['assigned_by', 'assigned_at', 'created_at', 'updated_at']
    verbose_name = '白银鼠鼠及以上专属KOOK房间'
    verbose_name_plural = '白银鼠鼠及以上专属KOOK房间（💎20,000解锁）'

    def has_add_permission(self, request, obj=None):
        if not request.user.is_superuser or obj is None:
            return False
        return not ClientVipKookRoom.objects.filter(profile=obj).exists()

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False


class AccountRestrictionLogInline(admin.TabularInline):
    model = AccountRestrictionLog
    extra = 0
    can_delete = False
    fields = ['from_status', 'to_status', 'suspended_until', 'reason', 'operator', 'created_at']
    readonly_fields = fields
    ordering = ['-created_at', '-id']
    verbose_name = '账户限制记录'
    verbose_name_plural = '账户限制历史（不可修改）'

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AccountRestrictionLog)
class AccountRestrictionLogAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'profile', 'from_status', 'to_status',
        'suspended_until', 'reason', 'operator', 'created_at',
    ]
    list_filter = ['from_status', 'to_status', 'created_at']
    search_fields = ['profile__nickname', 'profile__openid', 'reason', 'operator__username']
    list_select_related = ['profile', 'operator']
    ordering = ['-created_at', '-id']
    date_hierarchy = 'created_at'
    readonly_fields = [
        'profile', 'from_status', 'to_status', 'suspended_until',
        'reason', 'operator', 'created_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ClientProfile)
class ClientProfileAdmin(admin.ModelAdmin):
    form = ClientProfileAdminForm
    inlines = [ClientVipKookRoomInline, AccountRestrictionLogInline]
    list_display = [
        'id', 'nickname', 'openid', 'account_status', 'account_suspended_until',
        'vip_tier', 'cumulative_consumption', 'vip_kook_room_status',
        'player_status', 'created_at',
    ]
    search_fields = ['nickname', 'openid', 'user__username', 'vip_kook_room__kook_room_number']
    list_filter = ['account_status', 'vip_tier', 'player_status', 'created_at']
    list_select_related = ['vip_tier', 'user', 'account_restricted_by']
    readonly_fields = [
        'cumulative_consumption', 'vip_tier', 'vip_updated_at',
        'account_restricted_at', 'account_restricted_by',
        'created_at', 'updated_at',
    ]
    actions = ['refresh_vip_display', 'ensure_client_wallets']

    @admin.display(description='专属KOOK房间')
    def vip_kook_room_status(self, obj):
        room = ClientVipKookRoom.objects.filter(profile=obj).first()
        if not room:
            return '未配置'
        if not room.kook_room_number:
            return '等待填写'
        if not room.is_active:
            return f'{room.kook_room_number}（停用）'
        return room.kook_room_number

    def save_formset(self, request, form, formset, change):
        if formset.model is ClientVipKookRoom:
            instances = formset.save(commit=False)
            for instance in instances:
                instance.assigned_by = request.user
                instance.assigned_at = timezone.now()
                instance.save()
            formset.save_m2m()
            return
        super().save_formset(request, form, formset, change)

    def save_model(self, request, obj, form, change):
        previous = None
        if change and obj.pk:
            previous = ClientProfile.objects.filter(pk=obj.pk).values(
                'account_status', 'account_suspended_until', 'account_restriction_reason',
            ).first()

        account_changed = bool(previous) and any([
            previous['account_status'] != obj.account_status,
            previous['account_suspended_until'] != obj.account_suspended_until,
            previous['account_restriction_reason'] != obj.account_restriction_reason,
        ])
        if account_changed:
            obj.account_restricted_at = timezone.now()
            obj.account_restricted_by = request.user

        super().save_model(request, obj, form, change)

        if account_changed:
            AccountRestrictionLog.objects.create(
                profile=obj,
                from_status=previous['account_status'],
                to_status=obj.account_status,
                suspended_until=obj.account_suspended_until,
                reason=obj.account_restriction_reason or (
                    '管理员解除账户限制'
                    if obj.account_status == ClientProfile.ACCOUNT_STATUS_ACTIVE
                    else ''
                ),
                operator=request.user,
            )
            if obj.account_status != ClientProfile.ACCOUNT_STATUS_ACTIVE:
                Player.objects.filter(user=obj.user, is_online=True).update(
                    is_online=False,
                    updated_at=timezone.now(),
                )

    @admin.action(description='按当前累计消费重新计算VIP等级')
    def refresh_vip_display(self, request, queryset):
        from .vip import resolve_vip_tier

        updated = 0
        now = timezone.now()
        for profile in queryset:
            tier = resolve_vip_tier(profile.cumulative_consumption)
            if profile.vip_tier_id != getattr(tier, 'id', None):
                profile.vip_tier = tier
                profile.vip_updated_at = now
                profile.save(update_fields=['vip_tier', 'vip_updated_at', 'updated_at'])
                updated += 1
        self.message_user(request, f'已刷新 {queryset.count()} 位老板，变更 {updated} 位VIP等级')

    @admin.action(description='为选中的老板创建/补建钱包', permissions=['change'])
    def ensure_client_wallets(self, request, queryset):
        from apps.wallet.models import ClientWallet

        selected_count = queryset.count()
        created_count = 0
        for profile in queryset.iterator():
            _wallet, created = ClientWallet.objects.get_or_create(profile=profile)
            if created:
                created_count += 1

        existing_count = selected_count - created_count
        self.message_user(
            request,
            f'已检查 {selected_count} 位老板，新建 {created_count} 个钱包，'
            f'已有钱包 {existing_count} 个。',
            level=messages.SUCCESS,
        )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'boss-consumption/',
                self.admin_site.admin_view(self.boss_consumption_view),
                name='accounts_clientprofile_boss_consumption',
            ),
        ]
        return custom_urls + urls

    def boss_consumption_view(self, request):
        query = request.GET.get('q', '').strip()
        date_from = request.GET.get('date_from', '').strip()
        date_to = request.GET.get('date_to', '').strip()
        total_all = request.GET.get('total_all', '') == '1'

        results = []
        grand_total = 0
        grand_count = 0

        if query:
            profiles = ClientProfile.objects.select_related('vip_tier').filter(
                Q(nickname__icontains=query) | Q(openid__icontains=query)
            )
            for profile in profiles:
                ledger_filter = Q(profile=profile)
                order_filter = Q(boss_wechat=profile.openid, status=Order.STATUS_COMPLETED, paid=True)

                if not total_all:
                    if date_from:
                        try:
                            dt_from = datetime.strptime(date_from, '%Y-%m-%d')
                            ledger_filter &= Q(created_at__gte=dt_from)
                            order_filter &= Q(created_at__gte=dt_from)
                        except ValueError:
                            pass
                    if date_to:
                        try:
                            dt_to = datetime.strptime(date_to + ' 23:59:59', '%Y-%m-%d %H:%M:%S')
                            ledger_filter &= Q(created_at__lte=dt_to)
                            order_filter &= Q(created_at__lte=dt_to)
                        except ValueError:
                            pass

                ledgers = BossConsumptionLedger.objects.filter(ledger_filter)
                ledger_total = ledgers.aggregate(total=Sum('amount'))['total'] or 0
                orders = Order.objects.filter(order_filter)
                order_count = orders.aggregate(count=Count('id'))['count'] or 0
                total_spent = float(profile.cumulative_consumption if total_all else ledger_total)

                results.append({
                    'profile': profile,
                    'vip_tier': profile.vip_tier,
                    'total_spent': total_spent,
                    'order_count': order_count,
                    'recent_orders': orders.order_by('-created_at')[:10],
                    'recent_ledgers': ledgers.select_related('order', 'operator').order_by('-created_at')[:20],
                })
                grand_total += total_spent
                grand_count += order_count

            results.sort(key=lambda item: item['total_spent'], reverse=True)

        context = {
            'title': '老板消费与VIP查询',
            'query': query,
            'date_from': date_from,
            'date_to': date_to,
            'total_all': total_all,
            'results': results,
            'grand_total': grand_total,
            'grand_count': grand_count,
            'opts': self.model._meta,
            'has_permission': request.user.is_active and request.user.is_staff,
            'site_header': self.admin_site.site_header,
            'site_title': self.admin_site.site_title,
            'is_popup': False,
            'is_nav_sidebar_enabled': False,
        }
        return render(request, 'admin/boss_consumption.html', context)
