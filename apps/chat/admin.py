from django.contrib import admin

from .models import ChatMessage, ChatReadStatus, KeywordAlert, KeywordAlertLog


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ['id', 'order', 'sender_type', 'sender_name', 'message_type', 'created_at']
    search_fields = ['content', 'sender_name', 'order__order_no']


@admin.register(ChatReadStatus)
class ChatReadStatusAdmin(admin.ModelAdmin):
    list_display = ['id', 'order', 'reader_type', 'reader_id', 'last_read_at']


@admin.register(KeywordAlert)
class KeywordAlertAdmin(admin.ModelAdmin):
    list_display = ['id', 'keyword', 'is_active', 'created_at']
    list_editable = ['is_active']


@admin.register(KeywordAlertLog)
class KeywordAlertLogAdmin(admin.ModelAdmin):
    list_display = ['id', 'order', 'matched_keyword', 'sender_name', 'is_handled', 'created_at']
    list_filter = ['is_handled']
