from django.conf import settings
from django.db import models


class ClientProfile(models.Model):
    PLAYER_STATUS_NONE = 'none'
    PLAYER_STATUS_PENDING = 'pending'
    PLAYER_STATUS_APPROVED = 'approved'
    PLAYER_STATUS_REJECTED = 'rejected'

    PLAYER_STATUS_CHOICES = [
        (PLAYER_STATUS_NONE, '未申请'),
        (PLAYER_STATUS_PENDING, '审核中'),
        (PLAYER_STATUS_APPROVED, '已通过'),
        (PLAYER_STATUS_REJECTED, '已拒绝'),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='client_profile')
    openid = models.CharField(max_length=128, unique=True)
    unionid = models.CharField(max_length=128, blank=True, default='')
    nickname = models.CharField(max_length=100, blank=True, default='', unique=True)
    nickname_customized = models.BooleanField('用户自定义昵称', default=False)
    avatar_url = models.URLField(blank=True, default='')
    role = models.CharField(max_length=20, default='client')
    player_status = models.CharField(max_length=20, choices=PLAYER_STATUS_CHOICES, default=PLAYER_STATUS_NONE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'client_profiles'
        verbose_name = '客户资料'
        verbose_name_plural = '客户资料列表'

    def __str__(self):
        return self.nickname or self.openid
