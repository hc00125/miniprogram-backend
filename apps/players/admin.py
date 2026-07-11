from django.contrib import admin
from django.utils import timezone

from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType

from .models import Player, PlayerApplication


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'player_type', 'status', 'is_online', 'has_audio_intro', 'total_orders', 'created_at']
    list_filter = ['status', 'is_online', 'player_type']
    search_fields = ['name', 'contact_wechat', 'audio_intro_url', 'audio_intro_title']
    fieldsets = (
        ('基础信息', {
            'fields': ('user', 'name', 'player_type', 'status', 'is_online', 'contact_wechat', 'bio')
        }),
        ('音频自我介绍', {
            'fields': ('audio_intro_url', 'audio_intro_title'),
            'description': '将音频上传到服务器 media/player-audio/ 后，在这里填写完整 URL，例如 https://api.huc125.cn/media/player-audio/chen2.mp3'
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


@admin.register(PlayerApplication)
class PlayerApplicationAdmin(admin.ModelAdmin):
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
        # 当管理员在编辑页直接修改状态时，同步更新 ClientProfile 和 Player
        was_approved = obj.status == PlayerApplication.STATUS_APPROVED
        was_rejected = obj.status == PlayerApplication.STATUS_REJECTED

        if not obj.pk:
            obj.reviewed_by = request.user
        elif 'status' in form.changed_data:
            obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()

        super().save_model(request, obj, form, change)

        if was_approved and obj.user:
            # 同步 client_profile.player_status
            ClientProfile.objects.filter(user=obj.user).update(
                player_status=ClientProfile.PLAYER_STATUS_APPROVED,
            )
            # 创建 Player 记录（如果不存在）。真实姓名不写入公开陪玩资料。
            player = Player.objects.filter(user=obj.user).first()
            if not player:
                default_type = PlayerType.objects.filter(is_active=True).order_by('priority').first()
                Player.objects.create(
                    user=obj.user,
                    name=obj.name,
                    player_type=obj.player_type or default_type,
                    contact_wechat=obj.contact_wechat,
                    bio=obj.bio or '',
                    audio_intro_url=obj.audio_intro_url or '',
                    audio_intro_title=obj.audio_intro_title or '',
                    status=Player.STATUS_APPROVED,
                )
            else:
                player.audio_intro_url = obj.audio_intro_url or player.audio_intro_url
                player.audio_intro_title = obj.audio_intro_title or player.audio_intro_title
                player.save(update_fields=['audio_intro_url', 'audio_intro_title', 'updated_at'])
        elif was_rejected and obj.user:
            ClientProfile.objects.filter(user=obj.user).update(
                player_status=ClientProfile.PLAYER_STATUS_REJECTED,
            )

    @admin.action(description='批准选中的陪玩师申请')
    def approve_applications(self, request, queryset):
        approved = queryset.filter(status=PlayerApplication.STATUS_PENDING).select_related('user', 'player_type')
        approved_list = list(approved)
        user_ids = [app.user_id for app in approved_list]

        # 更新申请状态
        updated = approved.update(
            status=PlayerApplication.STATUS_APPROVED,
            reviewed_at=timezone.now(),
            reviewed_by=request.user
        )

        # 同步更新 client_profile.player_status
        ClientProfile.objects.filter(user_id__in=user_ids).update(
            player_status=ClientProfile.PLAYER_STATUS_APPROVED
        )

        # 为每个批准的用户创建 Player 记录（如果不存在）。真实姓名不写入公开陪玩资料。
        default_type = PlayerType.objects.filter(is_active=True).order_by('priority').first()
        for app in approved_list:
            player = Player.objects.filter(user=app.user).first()
            if not player:
                Player.objects.create(
                    user=app.user,
                    name=app.name,
                    player_type=app.player_type or default_type,
                    contact_wechat=app.contact_wechat,
                    bio=app.bio or '',
                    audio_intro_url=app.audio_intro_url or '',
                    audio_intro_title=app.audio_intro_title or '',
                    status=Player.STATUS_APPROVED,
                )
            else:
                player.audio_intro_url = app.audio_intro_url or player.audio_intro_url
                player.audio_intro_title = app.audio_intro_title or player.audio_intro_title
                player.save(update_fields=['audio_intro_url', 'audio_intro_title', 'updated_at'])

        self.message_user(request, f'已批准 {updated} 条申请')

    @admin.action(description='拒绝选中的陪玩师申请')
    def reject_applications(self, request, queryset):
        rejected = queryset.filter(status=PlayerApplication.STATUS_PENDING)

        # values_list 返回懒加载 QuerySet，必须在修改申请状态前取出用户 ID。
        # 否则 update 后原来的 pending 条件不再成立，后续会得到空集合。
        user_ids = list(rejected.values_list('user_id', flat=True))
        reviewed_at = timezone.now()

        updated = rejected.update(
            status=PlayerApplication.STATUS_REJECTED,
            reviewed_at=reviewed_at,
            reviewed_by=request.user
        )

        # 同步更新 client_profile.player_status
        ClientProfile.objects.filter(user_id__in=user_ids).update(
            player_status=ClientProfile.PLAYER_STATUS_REJECTED,
            updated_at=reviewed_at,
        )
        self.message_user(request, f'已拒绝 {updated} 条申请')
