from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType

from .approval import approve_player_application, validate_player_name_available
from .models import Player, PlayerApplication, PlayerProfileUpdateRequest


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'name', 'player_type', 'minimum_designated_player_type', 'status', 'is_online', 'is_publicly_visible',
        'can_accept_orders', 'can_be_designated', 'can_withdraw', 'has_audio_intro',
        'total_orders', 'created_at',
    ]
    list_filter = [
        'status', 'is_online', 'is_publicly_visible', 'can_accept_orders',
        'can_be_designated', 'can_withdraw', 'player_type', 'minimum_designated_player_type',
    ]
    search_fields = ['name', 'contact_wechat', 'audio_intro_url', 'audio_intro_title']
    fieldsets = (
        ('基础信息', {
            'fields': ('user', 'name', 'player_type', 'minimum_designated_player_type', 'status', 'is_online', 'contact_wechat', 'bio'),
            'description': '“陪玩类型”表示实际能力与接单资格；“最低指定计费类型”只决定老板点名指定时该名额的最低价格，留空则按陪玩自身类型计费。',
        }),
        ('功能权限', {
            'fields': ('can_accept_orders', 'can_be_designated', 'is_publicly_visible', 'can_withdraw'),
            'description': '关闭权限后，后端会同步阻止接单、指定、公开展示或提现，不只是隐藏前端按钮。',
        }),
        ('音频自我介绍', {
            'fields': ('audio_intro_url', 'audio_intro_title'),
            'description': '将音频上传到服务器 media/player-audio/ 后，在这里填写完整 URL。',
        }),
        ('数据统计', {
            'fields': ('total_orders', 'total_rating', 'rating_count', 'last_login'),
            'classes': ('collapse',),
        }),
        ('登录状态', {
            'fields': ('session_token', 'token_expires_at'),
            'classes': ('collapse',),
        }),
    )
    readonly_fields = ['created_at', 'updated_at']

    @admin.display(description='音频介绍')
    def has_audio_intro(self, obj):
        return '已配置' if obj.audio_intro_url else '未配置'


@admin.register(PlayerProfileUpdateRequest)
class PlayerProfileUpdateRequestAdmin(admin.ModelAdmin):
    list_display = ['id', 'player', 'status', 'has_audio_intro', 'submitted_at', 'reviewed_at', 'reviewed_by']
    list_filter = ['status', 'submitted_at', 'reviewed_at']
    search_fields = ['player__name', 'bio', 'audio_intro_title', 'reject_reason']
    actions = ['approve_updates', 'reject_updates']
    readonly_fields = ['player', 'submitted_at', 'reviewed_at', 'reviewed_by']
    fieldsets = (
        ('待审核资料', {
            'fields': ('player', 'bio', 'audio_intro_url', 'audio_intro_title', 'status')
        }),
        ('审核信息', {
            'fields': ('reject_reason', 'submitted_at', 'reviewed_at', 'reviewed_by')
        }),
    )

    @admin.display(description='音频介绍')
    def has_audio_intro(self, obj):
        return '已上传' if obj.audio_intro_url else '无音频'

    def apply_update(self, obj, reviewer):
        player = obj.player
        player.bio = obj.bio
        player.audio_intro_url = obj.audio_intro_url
        player.audio_intro_title = obj.audio_intro_title
        player.save(update_fields=['bio', 'audio_intro_url', 'audio_intro_title', 'updated_at'])
        obj.status = PlayerProfileUpdateRequest.STATUS_APPROVED
        obj.reject_reason = ''
        obj.reviewed_at = timezone.now()
        obj.reviewed_by = reviewer
        obj.save(update_fields=['status', 'reject_reason', 'reviewed_at', 'reviewed_by'])

    @admin.action(description='通过选中的陪玩资料修改申请')
    def approve_updates(self, request, queryset):
        count = 0
        for obj in queryset.filter(status=PlayerProfileUpdateRequest.STATUS_PENDING).select_related('player'):
            self.apply_update(obj, request.user)
            count += 1
        self.message_user(request, f'已通过 {count} 条资料修改申请')

    @admin.action(description='拒绝选中的陪玩资料修改申请')
    def reject_updates(self, request, queryset):
        count = queryset.filter(status=PlayerProfileUpdateRequest.STATUS_PENDING).update(
            status=PlayerProfileUpdateRequest.STATUS_REJECTED,
            reject_reason='管理员审核未通过，请修改后重新提交',
            reviewed_at=timezone.now(),
            reviewed_by=request.user,
        )
        self.message_user(request, f'已拒绝 {count} 条资料修改申请')

    def save_model(self, request, obj, form, change):
        requested_status = obj.status
        if change and 'status' in form.changed_data and requested_status == PlayerProfileUpdateRequest.STATUS_APPROVED:
            self.apply_update(obj, request.user)
            return
        if change and 'status' in form.changed_data and requested_status in {
            PlayerProfileUpdateRequest.STATUS_REJECTED,
            PlayerProfileUpdateRequest.STATUS_CANCELLED,
        }:
            obj.reviewed_at = timezone.now()
            obj.reviewed_by = request.user
        super().save_model(request, obj, form, change)


class PlayerApplicationAdminForm(forms.ModelForm):
    class Meta:
        model = PlayerApplication
        fields = '__all__'

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('status') != PlayerApplication.STATUS_APPROVED:
            return cleaned

        user = cleaned.get('user') or getattr(self.instance, 'user', None)
        if not user:
            self.add_error('user', '申请未绑定用户，不能通过审核')
            return cleaned

        try:
            cleaned['name'] = validate_player_name_available(
                cleaned.get('name'),
                user_id=user.id,
                include_applications=True,
                exclude_application_id=self.instance.pk,
            )
        except ValidationError as exc:
            self.add_error('name', exc.messages[0])

        if not cleaned.get('player_type') and not PlayerType.objects.filter(is_active=True).exists():
            self.add_error('player_type', '没有可用的陪玩类型，请先在后台启用陪玩类型')
        return cleaned


@admin.register(PlayerApplication)
class PlayerApplicationAdmin(admin.ModelAdmin):
    form = PlayerApplicationAdminForm
    list_display = [
        'id', 'name', 'masked_real_name', 'player_type', 'contact_wechat',
        'status', 'has_audio_intro', 'submitted_at', 'reviewed_at'
    ]
    list_filter = ['status', 'player_type']
    search_fields = ['name', 'real_name', 'contact_wechat', 'audio_intro_url', 'audio_intro_title']
    actions = ['approve_applications', 'reject_applications']
    readonly_fields = ['submitted_at', 'reviewed_at', 'reviewed_by']
    fieldsets = (
        ('申请身份信息', {
            'fields': ('user', 'real_name', 'name', 'player_type', 'contact_wechat', 'bio', 'status'),
            'description': '真实姓名仅用于平台内部审核；陪玩师名称会展示给老板和其他用户。'
        }),
        ('音频自我介绍', {
            'fields': ('audio_intro_url', 'audio_intro_title')
        }),
        ('审核信息', {
            'fields': ('reject_reason', 'remark', 'reviewed_by', 'submitted_at', 'reviewed_at')
        }),
    )

    @admin.display(description='真实姓名')
    def masked_real_name(self, obj):
        name = (obj.real_name or '').strip()
        if not name:
            return '未填写'
        if len(name) == 1:
            return name
        if len(name) == 2:
            return f'{name[0]}*'
        return f'{name[0]}{"*" * (len(name) - 2)}{name[-1]}'

    @admin.display(description='音频介绍')
    def has_audio_intro(self, obj):
        return '已上传' if obj.audio_intro_url else '未上传'

    def save_model(self, request, obj, form, change):
        requested_status = obj.status
        if not obj.pk:
            obj.reviewed_by = request.user
        elif 'status' in form.changed_data:
            obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()

        if requested_status == PlayerApplication.STATUS_APPROVED and obj.user:
            # 单条审批也必须和正式陪玩创建处于同一事务，避免只保存 approved 状态。
            with transaction.atomic():
                super().save_model(request, obj, form, change)
                approve_player_application(obj.pk, request.user)
            return

        super().save_model(request, obj, form, change)
        if requested_status == PlayerApplication.STATUS_REJECTED and obj.user:
            ClientProfile.objects.filter(user=obj.user).update(
                player_status=ClientProfile.PLAYER_STATUS_REJECTED,
                updated_at=timezone.now(),
            )

    @admin.action(description='批准选中的陪玩师申请')
    def approve_applications(self, request, queryset):
        application_ids = list(
            queryset.filter(status=PlayerApplication.STATUS_PENDING).values_list('id', flat=True)
        )
        approved_count = 0
        failures = []
        for application_id in application_ids:
            try:
                # 每条申请独立事务：一条失败不会污染状态，也不会阻止其他申请继续审批。
                _, application = approve_player_application(application_id, request.user)
                approved_count += 1
            except (ValidationError, PlayerApplication.DoesNotExist) as exc:
                application = PlayerApplication.objects.filter(pk=application_id).first()
                label = application.name if application else f'申请#{application_id}'
                message = '; '.join(getattr(exc, 'messages', [])) or str(exc)
                failures.append(f'{label}：{message}')

        if approved_count:
            self.message_user(request, f'已批准 {approved_count} 条申请')
        if failures:
            self.message_user(
                request,
                '以下申请未通过，状态保持待审核：' + ' | '.join(failures[:10]),
                level=messages.ERROR,
            )
        if not approved_count and not failures:
            self.message_user(request, '没有可批准的待审核申请', level=messages.WARNING)

    @admin.action(description='拒绝选中的陪玩师申请')
    def reject_applications(self, request, queryset):
        rejected = queryset.filter(status=PlayerApplication.STATUS_PENDING)
        user_ids = list(rejected.values_list('user_id', flat=True))
        reviewed_at = timezone.now()
        updated = rejected.update(
            status=PlayerApplication.STATUS_REJECTED,
            reviewed_at=reviewed_at,
            reviewed_by=request.user,
        )
        ClientProfile.objects.filter(user_id__in=user_ids).update(
            player_status=ClientProfile.PLAYER_STATUS_REJECTED,
            updated_at=reviewed_at,
        )
        self.message_user(request, f'已拒绝 {updated} 条申请')
