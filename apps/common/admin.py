from django.contrib import admin

from .models import MediaContentSecurityCheck


@admin.register(MediaContentSecurityCheck)
class MediaContentSecurityCheckAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'trace_id', 'user', 'media_type', 'status', 'suggest', 'label', 'errcode', 'created_at', 'updated_at',
    ]
    list_filter = ['status', 'media_type', 'scene', 'created_at']
    search_fields = ['trace_id', 'media_url', 'user__username', 'errmsg']
    readonly_fields = [
        'user', 'trace_id', 'media_url', 'media_type', 'scene', 'status', 'suggest', 'label',
        'errcode', 'errmsg', 'raw_result', 'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
