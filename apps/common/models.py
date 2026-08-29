from django.conf import settings
from django.db import models


class MediaContentSecurityCheck(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_PASS = 'pass'
    STATUS_REVIEW = 'review'
    STATUS_RISKY = 'risky'
    STATUS_ERROR = 'error'

    STATUS_CHOICES = [
        (STATUS_PENDING, '检测中'),
        (STATUS_PASS, '已通过'),
        (STATUS_REVIEW, '需复核'),
        (STATUS_RISKY, '有风险'),
        (STATUS_ERROR, '检测异常'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='media_content_security_checks',
        verbose_name='用户',
    )
    trace_id = models.CharField(max_length=128, unique=True, db_index=True, verbose_name='微信 Trace ID')
    media_url = models.CharField(max_length=1000, db_index=True, verbose_name='媒体 URL')
    media_type = models.PositiveSmallIntegerField(verbose_name='媒体类型')
    scene = models.PositiveSmallIntegerField(default=1, verbose_name='检测场景')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    suggest = models.CharField(max_length=20, blank=True, default='', verbose_name='微信建议')
    label = models.CharField(max_length=64, blank=True, default='', verbose_name='风险标签')
    errcode = models.IntegerField(default=0, verbose_name='微信错误码')
    errmsg = models.CharField(max_length=300, blank=True, default='', verbose_name='微信错误信息')
    raw_result = models.JSONField(default=dict, blank=True, verbose_name='原始回调结果')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'media_content_security_checks'
        verbose_name = '微信媒体内容安全检测'
        verbose_name_plural = '微信媒体内容安全检测'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'media_url', 'media_type'], name='media_sec_user_url_idx'),
        ]

    @property
    def is_passed(self):
        return self.status == self.STATUS_PASS

    def __str__(self):
        return f'{self.trace_id} - {self.get_status_display()}'
