from django.contrib import admin
from django.utils import timezone

from .models import PlayerServiceListing


@admin.register(PlayerServiceListing)
class PlayerServiceListingAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'player', 'package_name', 'spec', 'status', 'is_available',
        'sort_order', 'reviewed_at', 'created_at',
    ]
    list_filter = ['status', 'is_available', 'spec__package', 'spec__required_player_type']
    search_fields = ['player__name', 'spec__package__name', 'spec__name', 'custom_description']
    list_select_related = ['player', 'spec__package', 'spec__required_player_type', 'reviewed_by']
    readonly_fields = ['created_at', 'updated_at', 'reviewed_at', 'reviewed_by']
    actions = ['approve_listings', 'reject_listings', 'offline_listings']

    @admin.display(description='共享商品')
    def package_name(self, obj):
        return obj.spec.package.name

    def save_model(self, request, obj, form, change):
        if change and 'status' in form.changed_data:
            obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()
            if obj.status == PlayerServiceListing.STATUS_APPROVED:
                obj.rejection_reason = ''
                obj.is_available = True
            elif obj.status == PlayerServiceListing.STATUS_OFFLINE:
                obj.is_available = False
        super().save_model(request, obj, form, change)

    @admin.action(description='审核通过并上架')
    def approve_listings(self, request, queryset):
        count = queryset.update(
            status=PlayerServiceListing.STATUS_APPROVED,
            is_available=True,
            rejection_reason='',
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, f'已通过并上架 {count} 项服务')

    @admin.action(description='拒绝上架申请')
    def reject_listings(self, request, queryset):
        count = queryset.update(
            status=PlayerServiceListing.STATUS_REJECTED,
            is_available=False,
            rejection_reason='管理员审核未通过，请调整后重新提交',
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, f'已拒绝 {count} 项服务')

    @admin.action(description='下架所选服务')
    def offline_listings(self, request, queryset):
        count = queryset.update(
            status=PlayerServiceListing.STATUS_OFFLINE,
            is_available=False,
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, f'已下架 {count} 项服务')
