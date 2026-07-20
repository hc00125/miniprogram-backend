from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Player(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_DISABLED = 'disabled'

    STATUS_CHOICES = [
        (STATUS_PENDING, '审核中'),
        (STATUS_APPROVED, '已通过'),
        (STATUS_REJECTED, '已拒绝'),
        (STATUS_DISABLED, '已禁用'),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True, related_name='player_profile')
    name = models.CharField(max_length=50, unique=True)
    player_type = models.ForeignKey('catalog.PlayerType', on_delete=models.PROTECT, related_name='players')
    minimum_designated_player_type = models.ForeignKey(
        'catalog.PlayerType',
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name='minimum_designated_players',
        verbose_name='最低指定计费类型',
        help_text='老板指定该陪玩时，至少按此类型对应的商品规格单人价计费；留空时按陪玩自身类型计费。',
    )
    total_orders = models.IntegerField(default=0)
    total_rating = models.FloatField(default=0)
    rating_count = models.IntegerField(default=0)
    is_online = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_APPROVED)
    contact_wechat = models.CharField(max_length=100, blank=True, default='')
    bio = models.TextField(blank=True, default='')
    audio_intro_url = models.CharField(max_length=500, blank=True, default='', verbose_name='音频自我介绍URL', help_text='填写 mp3/m4a 等音频地址，例如 https://api.huc125.cn/media/player-audio/xxx.mp3')
    audio_intro_title = models.CharField(max_length=100, blank=True, default='', verbose_name='音频标题', help_text='可选，例如：我的自我介绍')
    can_accept_orders = models.BooleanField(default=True, verbose_name='允许接单')
    can_be_designated = models.BooleanField(default=True, verbose_name='允许被指定')
    is_publicly_visible = models.BooleanField(default=True, verbose_name='在陪玩列表展示')
    can_withdraw = models.BooleanField(default=True, verbose_name='允许提现')
    last_login = models.DateTimeField(blank=True, null=True)
    session_token = models.CharField(max_length=128, blank=True, null=True, db_index=True)
    token_expires_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'players'
        verbose_name = '陪玩师'
        verbose_name_plural = '陪玩师列表'

    @property
    def type_id(self):
        return self.player_type_id

    @property
    def designated_billing_type(self):
        return self.minimum_designated_player_type or self.player_type

    @property
    def avg_rating(self):
        if self.rating_count <= 0:
            return 0
        return round(self.total_rating / self.rating_count, 1)

    def refresh_legacy_token(self, token):
        self.session_token = token
        self.token_expires_at = timezone.now() + timedelta(days=30)
        self.is_online = True
        self.last_login = timezone.now()

    def __str__(self):
        return self.name


class PlayerApplication(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'

    STATUS_CHOICES = [
        (STATUS_PENDING, '待审核'),
        (STATUS_APPROVED, '已通过'),
        (STATUS_REJECTED, '已拒绝'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='player_applications')
    name = models.CharField(max_length=50)
    real_name = models.CharField(
        max_length=30,
        verbose_name='真实姓名',
        help_text='仅供平台审核使用，不对老板或其他用户公开',
    )
    player_type = models.ForeignKey('catalog.PlayerType', on_delete=models.PROTECT, blank=True, null=True, related_name='applications')
    contact_wechat = models.CharField(max_length=100)
    bio = models.TextField(blank=True, default='')
    audio_intro_url = models.CharField(max_length=500, blank=True, default='', verbose_name='音频自我介绍URL')
    audio_intro_title = models.CharField(max_length=100, blank=True, default='', verbose_name='音频标题')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    reject_reason = models.CharField(max_length=300, blank=True, default='')
    remark = models.CharField(max_length=300, blank=True, default='')
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True, related_name='reviewed_player_applications')
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'player_applications'
        verbose_name = '陪玩师申请'
        verbose_name_plural = '陪玩师申请列表'
        ordering = ['-submitted_at']

    def __str__(self):
        return f'{self.name} {self.status}'


class PlayerProfileUpdateRequest(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_PENDING, '待审核'),
        (STATUS_APPROVED, '已通过'),
        (STATUS_REJECTED, '已拒绝'),
        (STATUS_CANCELLED, '已取消'),
    ]

    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='profile_update_requests', verbose_name='陪玩师')
    bio = models.TextField(blank=True, default='', verbose_name='新个人简介')
    audio_intro_url = models.CharField(max_length=500, blank=True, default='', verbose_name='新音频URL')
    audio_intro_title = models.CharField(max_length=100, blank=True, default='', verbose_name='新音频标题')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    reject_reason = models.CharField(max_length=300, blank=True, default='')
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(blank=True, null=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='reviewed_player_profile_updates',
    )

    class Meta:
        db_table = 'player_profile_update_requests'
        verbose_name = '陪玩资料修改申请'
        verbose_name_plural = '陪玩资料修改申请'
        ordering = ['-submitted_at']
        constraints = [
            models.UniqueConstraint(
                fields=['player'],
                condition=Q(status='pending'),
                name='uniq_pending_player_profile_update',
            ),
        ]

    def __str__(self):
        return f'{self.player.name} - {self.get_status_display()}'


from .escort_models import PlayerEscortApplication, PlayerEscortQualification  # noqa: E402,F401
