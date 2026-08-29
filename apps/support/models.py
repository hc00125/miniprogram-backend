from django.core.exceptions import ValidationError
from django.db import models


class SupportChannel(models.Model):
    TYPE_WECHAT_OFFICIAL = 'wechat_official'
    TYPE_WECHAT_PERSONAL = 'wechat_personal'
    TYPE_CHOICES = [
        (TYPE_WECHAT_OFFICIAL, '微信官方客服'),
        (TYPE_WECHAT_PERSONAL, '人工客服微信'),
    ]

    AUDIENCE_ALL = 'all'
    AUDIENCE_BOSS = 'boss'
    AUDIENCE_PLAYER = 'player'
    AUDIENCE_CHOICES = [
        (AUDIENCE_ALL, '全部用户'),
        (AUDIENCE_BOSS, '仅老板'),
        (AUDIENCE_PLAYER, '仅陪玩'),
    ]

    name = models.CharField(max_length=50, verbose_name='渠道名称')
    channel_type = models.CharField(max_length=30, choices=TYPE_CHOICES, db_index=True, verbose_name='渠道类型')
    wechat_id = models.CharField(max_length=100, blank=True, default='', verbose_name='客服微信号')
    service_hours = models.CharField(max_length=100, blank=True, default='', verbose_name='服务时间')
    description = models.CharField(max_length=200, blank=True, default='', verbose_name='展示说明')
    audience = models.CharField(
        max_length=20,
        choices=AUDIENCE_CHOICES,
        default=AUDIENCE_ALL,
        db_index=True,
        verbose_name='展示对象',
    )
    sort_order = models.IntegerField(default=0, verbose_name='排序')
    is_active = models.BooleanField(default=True, db_index=True, verbose_name='是否展示')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'support_channels'
        verbose_name = '客服渠道'
        verbose_name_plural = '客服渠道'
        ordering = ['sort_order', 'id']

    def clean(self):
        super().clean()
        self.name = str(self.name or '').strip()
        self.wechat_id = str(self.wechat_id or '').strip()
        self.service_hours = str(self.service_hours or '').strip()
        self.description = str(self.description or '').strip()
        if self.channel_type == self.TYPE_WECHAT_PERSONAL and not self.wechat_id:
            raise ValidationError({'wechat_id': '人工客服微信必须填写微信号'})

    def __str__(self):
        detail = self.wechat_id if self.channel_type == self.TYPE_WECHAT_PERSONAL else self.get_channel_type_display()
        return f'{self.name} - {detail}'
