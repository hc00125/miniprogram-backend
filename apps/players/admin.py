from django.contrib import admin
from django.utils import timezone

from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType

from .models import Player, PlayerApplication


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'player_type', 'status', 'is_online', 'total_orders', 'created_at']
    list_filter = ['status', 'is_online', 'player_type']
    search_fields = ['name', 'contact_wechat']


@admin.register(PlayerApplication)
class PlayerApplicationAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'player_type', 'contact_wechat', 'status', 'submitted_at', 'reviewed_at']
    list_filter = ['status', 'player_type']
    search_fields = ['name', 'contact_wechat']
    actions = ['approve_applications', 'reject_applications']

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

        # 为每个批准的用户创建 Player 记录（如果不存在）
        default_type = PlayerType.objects.filter(is_active=True).order_by('priority').first()
        for app in approved_list:
            if not Player.objects.filter(user=app.user).exists():
                Player.objects.create(
                    user=app.user,
                    name=app.name,
                    player_type=app.player_type or default_type,
                    contact_wechat=app.contact_wechat,
                    bio=app.bio or '',
                    status=Player.STATUS_APPROVED,
                )

        self.message_user(request, f'已批准 {updated} 条申请')

    @admin.action(description='拒绝选中的陪玩师申请')
    def reject_applications(self, request, queryset):
        rejected = queryset.filter(status=PlayerApplication.STATUS_PENDING)
        user_ids = rejected.values_list('user_id', flat=True)
        updated = rejected.update(
            status=PlayerApplication.STATUS_REJECTED,
            reviewed_at=timezone.now(),
            reviewed_by=request.user
        )
        # 同步更新 client_profile.player_status
        ClientProfile.objects.filter(user_id__in=user_ids).update(
            player_status=ClientProfile.PLAYER_STATUS_REJECTED
        )
        self.message_user(request, f'已拒绝 {updated} 条申请')
