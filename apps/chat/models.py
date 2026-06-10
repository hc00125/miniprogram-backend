from django.db import models


class ChatMessage(models.Model):
    order = models.ForeignKey('orders.Order', on_delete=models.CASCADE, related_name='chat_messages')
    sender_type = models.CharField(max_length=10)
    sender_id = models.CharField(max_length=50)
    sender_name = models.CharField(max_length=50)
    content = models.TextField()
    message_type = models.CharField(max_length=20, default='text')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'chat_messages'
        verbose_name = '聊天消息'
        verbose_name_plural = '聊天消息列表'
        ordering = ['created_at']


class ChatReadStatus(models.Model):
    order = models.ForeignKey('orders.Order', on_delete=models.CASCADE, related_name='chat_read_statuses')
    reader_type = models.CharField(max_length=10)
    reader_id = models.CharField(max_length=50)
    last_read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'chat_read_status'
        verbose_name = '聊天阅读状态'
        verbose_name_plural = '聊天阅读状态列表'
        unique_together = [('order', 'reader_type', 'reader_id')]


class KeywordAlert(models.Model):
    keyword = models.CharField(max_length=100)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'keyword_alerts'
        verbose_name = '关键词告警'
        verbose_name_plural = '关键词告警列表'


class KeywordAlertLog(models.Model):
    keyword = models.ForeignKey(KeywordAlert, on_delete=models.SET_NULL, blank=True, null=True)
    order = models.ForeignKey('orders.Order', on_delete=models.CASCADE, related_name='keyword_alert_logs')
    message = models.ForeignKey(ChatMessage, on_delete=models.SET_NULL, blank=True, null=True)
    sender_name = models.CharField(max_length=50, blank=True, default='')
    matched_keyword = models.CharField(max_length=100, blank=True, default='')
    content_snippet = models.CharField(max_length=200, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    is_handled = models.BooleanField(default=False)

    class Meta:
        db_table = 'keyword_alert_logs'
        verbose_name = '关键词告警日志'
        verbose_name_plural = '关键词告警日志列表'