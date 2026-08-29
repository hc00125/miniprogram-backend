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


class PlayerOrderNoticeSubscription(models.Model):
    """One-time WeChat subscription-message permissions granted by a player."""

    player = models.OneToOneField(
        Player,
        on_delete=models.CASCADE,
        related_name='order_notice_subscription',
        verbose_name='陪玩师',
    )
    template_id = models.CharField(max_length=128, blank=True, default='', verbose_name='订阅消息模板 ID')
    available_count = models.PositiveIntegerField(default=0, verbose_name='可发送次数')
    last_subscribed_at = models.DateTimeField(blank=True, null=True, verbose_name='最近授权时间')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'player_order_notice_subscriptions'
        verbose_name = '陪玩师接单订阅消息授权'
        verbose_name_plural = '陪玩师接单订阅消息授权'

    def __str__(self):
        return f'{self.player.name} - {self.available_count}'


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


class PlayerServiceListing(models.Model):
    """A player's self-service listing of one platform-owned shared specification."""

    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_OFFLINE = 'offline'
    STATUS_CHOICES = [
        (STATUS_PENDING, '待审核'),
        (STATUS_APPROVED, '已上架'),
        (STATUS_REJECTED, '已拒绝'),
        (STATUS_OFFLINE, '已下架'),
    ]

    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name='service_listings',
        verbose_name='陪玩师',
    )
    spec = models.ForeignKey(
        'catalog.PackageSpec',
        on_delete=models.PROTECT,
        related_name='player_service_listings',
        verbose_name='共享服务规格',
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    is_available = models.BooleanField(default=True, db_index=True, verbose_name='当前可预约')
    custom_description = models.CharField(max_length=300, blank=True, default='', verbose_name='个人服务说明')
    sort_order = models.IntegerField(default=0, verbose_name='个人排序')
    rejection_reason = models.CharField(max_length=300, blank=True, default='', verbose_name='拒绝原因')
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='reviewed_player_service_listings',
        verbose_name='审核人',
    )
    reviewed_at = models.DateTimeField(blank=True, null=True, verbose_name='审核时间')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'player_service_listings'
        verbose_name = '陪玩共享服务上架'
        verbose_name_plural = '陪玩共享服务上架'
        ordering = ['sort_order', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['player', 'spec'],
                name='uniq_player_shared_service_spec',
            ),
        ]
        indexes = [
            models.Index(fields=['player', 'status', 'is_available'], name='player_listing_public_idx'),
        ]

    @property
    def package(self):
        return self.spec.package

    def automatic_approval_error(self):
        player = self.player
        spec = self.spec
        package = spec.package
        if player.status != Player.STATUS_APPROVED:
            return '陪玩师账号尚未通过审核'
        if not player.can_accept_orders or not player.can_be_designated:
            return '陪玩师当前未开放接单或指定权限'
        if not package.is_active or not spec.is_active:
            return '共享商品或规格已下架'
        required_type = spec.required_player_type
        billing_type = player.designated_billing_type
        if required_type and (not billing_type or billing_type.priority < required_type.priority):
            return f'当前陪玩等级未达到“{required_type.name}”要求'
        if package.requires_escort_qualification:
            return '护航类服务需要管理员人工审核资格'
        return ''

    @property
    def is_publicly_sellable(self):
        return bool(
            self.status == self.STATUS_APPROVED
            and self.is_available
            and self.player.status == Player.STATUS_APPROVED
            and self.player.can_accept_orders
            and self.player.can_be_designated
            and self.player.is_publicly_visible
            and self.spec.is_active
            and self.spec.package.is_active
        )

    def __str__(self):
        return f'{self.player.name} - {self.spec}'


from .escort_models import PlayerEscortApplication, PlayerEscortQualification  # noqa: E402,F401
