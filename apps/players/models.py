from datetime import timedelta

from django.conf import settings
from django.db import models
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
    total_orders = models.IntegerField(default=0)
    total_rating = models.FloatField(default=0)
    rating_count = models.IntegerField(default=0)
    is_online = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_APPROVED)
    contact_wechat = models.CharField(max_length=100, blank=True, default='')
    bio = models.TextField(blank=True, default='')
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
        (STATUS_PENDING, '审核中'),
        (STATUS_APPROVED, '已通过'),
        (STATUS_REJECTED, '已拒绝'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='player_applications')
    name = models.CharField(max_length=50)
    player_type = models.ForeignKey('catalog.PlayerType', on_delete=models.PROTECT, blank=True, null=True, related_name='applications')
    contact_wechat = models.CharField(max_length=100)
    bio = models.TextField(blank=True, default='')
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
