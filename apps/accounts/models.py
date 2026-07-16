from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q


class VipTier(models.Model):
    code = models.CharField(max_length=30, unique=True, verbose_name='等级代码')
    name = models.CharField(max_length=50, verbose_name='等级名称')
    min_consumption = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name='最低累计消费(元)',
    )
    benefits = models.JSONField(default=list, blank=True, verbose_name='会员权益')
    feature_codes = models.JSONField(default=list, blank=True, verbose_name='可执行权益代码')
    badge_color = models.CharField(max_length=20, default='green', verbose_name='徽章主题')
    sort_order = models.IntegerField(default=0, verbose_name='排序')
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'vip_tiers'
        verbose_name = '老板VIP等级'
        verbose_name_plural = '老板VIP等级'
        ordering = ['min_consumption', 'sort_order', 'id']
        constraints = [
            models.CheckConstraint(
                check=Q(min_consumption__gte=Decimal('0.00')),
                name='vip_tier_min_consumption_non_negative',
            ),
        ]

    def __str__(self):
        return f'{self.name}（满¥{self.min_consumption}）'


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
    cumulative_consumption = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name='累计有效消费(元)',
    )
    vip_tier = models.ForeignKey(
        VipTier,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='members',
        verbose_name='当前VIP等级',
    )
    vip_updated_at = models.DateTimeField(blank=True, null=True, verbose_name='VIP更新时间')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'client_profiles'
        verbose_name = '客户资料'
        verbose_name_plural = '客户资料列表'
        constraints = [
            models.CheckConstraint(
                check=Q(cumulative_consumption__gte=Decimal('0.00')),
                name='client_consumption_non_negative',
            ),
        ]

    def __str__(self):
        return self.nickname or self.openid


class ClientVipKookRoom(models.Model):
    profile = models.OneToOneField(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name='vip_kook_room',
        verbose_name='老板',
    )
    kook_room_number = models.CharField(
        max_length=100,
        blank=True,
        default='',
        verbose_name='专属KOOK房间号',
    )
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='assigned_vip_kook_rooms',
        verbose_name='配置超管',
    )
    assigned_at = models.DateTimeField(blank=True, null=True, verbose_name='配置时间')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'client_vip_kook_rooms'
        verbose_name = '老板专属KOOK房间'
        verbose_name_plural = '老板专属KOOK房间'
        ordering = ['-updated_at', '-id']

    def __str__(self):
        room_number = self.kook_room_number or '待配置'
        return f'{self.profile} - {room_number}'


class BossConsumptionLedger(models.Model):
    TYPE_ORDER = 'order'
    TYPE_REFUND = 'refund'
    TYPE_MANUAL = 'manual'
    TYPE_BACKFILL = 'backfill'

    TYPE_CHOICES = [
        (TYPE_ORDER, '订单消费'),
        (TYPE_REFUND, '退款扣减'),
        (TYPE_MANUAL, '人工调整'),
        (TYPE_BACKFILL, '历史补录'),
    ]

    profile = models.ForeignKey(
        ClientProfile,
        on_delete=models.PROTECT,
        related_name='consumption_ledgers',
        verbose_name='老板',
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='消费变动(元)')
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='变动后累计消费(元)')
    source_type = models.CharField(max_length=20, choices=TYPE_CHOICES, db_index=True, verbose_name='来源')
    order = models.ForeignKey(
        'orders.Order',
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name='boss_consumption_ledgers',
        verbose_name='关联订单',
    )
    reference_id = models.CharField(max_length=64, blank=True, default='', db_index=True, verbose_name='来源编号')
    reason = models.CharField(max_length=500, blank=True, default='', verbose_name='原因/备注')
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='boss_consumption_operations',
        verbose_name='操作管理员',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'boss_consumption_ledgers'
        verbose_name = '老板消费流水'
        verbose_name_plural = '老板消费流水'
        ordering = ['-created_at', '-id']
        constraints = [
            models.CheckConstraint(check=~Q(amount=Decimal('0.00')), name='boss_consumption_amount_non_zero'),
            models.CheckConstraint(
                check=Q(balance_after__gte=Decimal('0.00')),
                name='boss_consumption_balance_non_negative',
            ),
            models.UniqueConstraint(
                fields=('profile', 'source_type', 'reference_id'),
                condition=~Q(reference_id=''),
                name='uniq_boss_consumption_reference',
            ),
        ]

    def __str__(self):
        sign = '+' if self.amount > 0 else ''
        return f'{self.profile} {sign}{self.amount}元'
