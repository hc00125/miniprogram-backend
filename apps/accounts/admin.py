from django.contrib import admin

from .models import ClientProfile


@admin.register(ClientProfile)
class ClientProfileAdmin(admin.ModelAdmin):
    list_display = ['id', 'nickname', 'openid', 'player_status', 'created_at']
    search_fields = ['nickname', 'openid']
    list_filter = ['player_status']
