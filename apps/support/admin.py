from django.contrib import admin

from .models import SupportChannel


@admin.register(SupportChannel)
class SupportChannelAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'channel_type', 'wechat_id', 'audience',
        'service_hours', 'sort_order', 'is_active', 'updated_at',
    ]
    list_editable = ['sort_order', 'is_active']
    list_filter = ['channel_type', 'audience', 'is_active']
    search_fields = ['name', 'wechat_id', 'description', 'service_hours']
    ordering = ['sort_order', 'id']
    readonly_fields = ['created_at', 'updated_at']
    fieldsets = [
        ('基础信息', {
            'fields': ['name', 'channel_type', 'wechat_id', 'description'],
        }),
        ('展示规则', {
            'fields': ['audience', 'service_hours', 'sort_order', 'is_active'],
        }),
        ('记录信息', {
            'fields': ['created_at', 'updated_at'],
        }),
    ]

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False
