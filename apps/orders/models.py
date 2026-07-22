from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Order(models.Model):
    STATUS_WAITING = '待接单'
    STATUS_PENDING_PAYMENT = '待支付'
    STATUS_READY_TO_START = '待开打'
    STATUS_IN_PROGRESS = '进行中'
    STATUS_COMPLETED = '已完成'
    STATUS_CANCELLED = '已取消'

    STATUS_CHOICES = [
        (STATUS_WAITING, STATUS_WAITING),
        (STATUS_PENDING_PAYMENT, STATUS_PENDING_PAYMENT),
        (STATUS_READY_TO_START, STATUS_READY_TO_START),
        (STATUS_IN_PROGRESS, STATUS_IN_PROGRESS),
        (STATUS_COMPLETED, STATUS_COMPLETED),
        (STATUS_CANCELLED, STATUS_CANCELLED),
    ]

    ORDER_TYPE_NORMAL = 'normal'
    ORDER_TYPE_RENEWAL = 'renewal'
    ORDER_TYPE_CHOICES = [
        (ORDER_TYPE_NORMAL, '普通订单'),
        (ORDER_TYPE_RENEWAL, '续单'),
    ]

    PRICING_MODE_LEGACY = 'legacy'
    PRICING_MODE_COMPOSITION = 'composition'
    PRICING_MODE_CHOICES = [
        (PRICING_MODE_LEGACY, '普通定价'),
        (PRICING_MODE_COMPOSITION, '指定陪玩组合定价'),
    ]

    FULFILLMENT_MODE_PUBLIC = 'public'
    FULFILLMENT_MODE_TARGETED = 'targeted'
    FULFILLMENT_MODE_CHOICES = [
        (FULFILLMENT_MODE_PUBLIC, '公开抢单'),
        (FULFILLMENT_MODE_TARGETED, '指定陪玩师商品'),
    ]

    order_no = models.CharField(max_length=20, unique=True, db_index=True)
    boss_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True, related_name='boss_orders')
    boss_wechat = models.CharField(max_length=50)
    game_id = models.CharField(max_length=100, blank=True, null=True)
    package = models.ForeignKey('catalog.Package', on_delete=models.PROTECT, related_name='orders')
    spec_id = models.IntegerField(blank=True, null=True, verbose_name='规格ID')
    package_name_snapshot = models.CharField(max_length=100, blank=True, null=True, verbose_name='下单时商品名快照')
    spec_name_snapshot = models.CharField(max_length=100, blank=True, null=True, verbose_name='下单时规格名快照')
    spec_price_snapshot = models.FloatField(blank=True, null=True, verbose_name='下单时规格价快照')
    addon = models.ForeignKey('catalog.Addon', on_delete=models.SET_NULL, blank=True, null=True, related_name='orders')
    addon_details = models.JSONField(blank=True, null=True)
    required_players = models.IntegerField()
    designated_types = models.JSONField(blank=True, null=True)
    designated_players = models.JSONField(blank=True, null=True)
    boss_note = models.TextField(blank=True, null=True)
    total_price_per_hour = models.FloatField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_WAITING)
    start_time = models.DateTimeField(blank=True, null=True)
    end_time = models.DateTimeField(blank=True, null=True)
    duration_minutes = models.IntegerField(blank=True, null=True)
    total_amount = models.FloatField(blank=True, null=True)
    paid = models.BooleanField(default=False)
    payment_method = models.CharField(max_length=20, blank=True, null=True)
    payment_confirmed_at = models.DateTimeField(blank=True, null=True)
    payment_confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True, related_name='confirmed_payments')
    is_custom = models.BooleanField(default=False)
    custom_price = models.FloatField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    canceled_at = models.DateTimeField(blank=True, null=True)
    cancel_reason = models.CharField(max_length=200, blank=True, null=True)
    booked_hours = models.FloatField(default=1.0)
    timer_started_at = models.DateTimeField(blank=True, null=True)
    paused_duration = models.IntegerField(default=0)
    is_paused = models.BooleanField(default=False)
    last_paused_at = models.DateTimeField(blank=True, null=True)
    kook_room_number = models.CharField(max_length=100, blank=True, default='', verbose_name='KOOK房间号')
    kook_room_updated_at = models.DateTimeField(blank=True, null=True, verbose_name='KOOK房间号更新时间')
    kook_room_updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True, related_name='updated_kook_rooms', verbose_name='KOOK房间号填写人')

    order_type = models.CharField(
        max_length=20,
        choices=ORDER_TYPE_CHOICES,
        default=ORDER_TYPE_NORMAL,
        db_index=True,
        verbose_name='订单类型',
    )
    parent_order = models.ForeignKey(
        'self',
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name='renewal_orders',
        verbose_name='原订单',
    )
    renewal_index = models.PositiveIntegerField(default=0, verbose_name='续单序号')

    # 组合定价订单不依赖陪玩师身份定价。这里保留静态组合 SKU 与虚拟商品规格的
    # 不可变快照，避免后台后续调整商品关系影响已发出的订单。
    pricing_mode = models.CharField(
        max_length=20,
        choices=PRICING_MODE_CHOICES,
        default=PRICING_MODE_LEGACY,
        db_index=True,
        verbose_name='定价模式',
    )
    composition_sku_id = models.PositiveBigIntegerField(
        blank=True,
        null=True,
        db_index=True,
        verbose_name='组合结算SKU ID快照',
    )
    composition_key = models.CharField(
        max_length=180,
        blank=True,
        default='',
        verbose_name='组合结算SKU键快照',
    )
    composition_virtual_spec_id = models.PositiveBigIntegerField(
        blank=True,
        null=True,
        verbose_name='组合虚拟商品规格ID快照',
    )
    composition_price_per_hour = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name='组合每小时价格快照',
    )
    composition_pricing_error = models.CharField(
        max_length=300,
        blank=True,
        default='',
        verbose_name='组合定价配置错误',
    )
    fulfillment_mode = models.CharField(
        max_length=20,
        choices=FULFILLMENT_MODE_CHOICES,
        default=FULFILLMENT_MODE_PUBLIC,
        db_index=True,
        verbose_name='履约方式',
    )
    target_player = models.ForeignKey(
        'players.Player',
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name='targeted_orders',
        verbose_name='指定服务陪玩师',
    )
    target_player_name_snapshot = models.CharField(
        max_length=100,
        blank=True,
        default='',
        verbose_name='指定服务陪玩师名称快照',
    )

    class Meta:
        db_table = 'orders'
        verbose_name = '订单'
        verbose_name_plural = '订单列表'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['parent_order', 'renewal_index'],
                condition=Q(parent_order__isnull=False),
                name='uniq_order_renewal_index',
            ),
        ]

    def __str__(self):
        return self.order_no


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    package = models.ForeignKey('catalog.Package', on_delete=models.PROTECT, related_name='order_items')
    spec = models.ForeignKey('catalog.PackageSpec', on_delete=models.SET_NULL, blank=True, null=True, related_name='order_items')
    package_name = models.CharField(max_length=120)
    spec_name = models.CharField(max_length=120, blank=True, default='')
    spec_display_name = models.CharField(max_length=120, blank=True, default='')
    unit_price = models.FloatField()
    quantity = models.PositiveIntegerField(default=1)
    amount = models.FloatField()
    image_url = models.CharField(max_length=500, blank=True, null=True)
    description = models.CharField(max_length=300, blank=True, null=True)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'order_items'
        verbose_name = '订单商品'
        verbose_name_plural = '订单商品列表'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return f'{self.order.order_no} - {self.package_name} x{self.quantity}'


class OrderPricingLine(models.Model):
    """组合定价订单的不可变价格明细。

    `player_id_snapshot` 仅用于审计和界面展示；组合 SKU 的匹配与金额只依赖
    陪玩类型计数，不会因具体陪玩师身份形成新的价格。
    """

    SOURCE_PUBLIC = 'public'
    SOURCE_DESIGNATED = 'designated'
    SOURCE_CHOICES = [
        (SOURCE_PUBLIC, '公开抢单名额'),
        (SOURCE_DESIGNATED, '指定陪玩名额'),
    ]

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='pricing_lines')
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    player_id_snapshot = models.PositiveBigIntegerField(blank=True, null=True)
    player_name_snapshot = models.CharField(max_length=100, blank=True, default='')
    billing_player_type_id = models.PositiveBigIntegerField(blank=True, null=True)
    billing_player_type_name = models.CharField(max_length=60, blank=True, default='')
    package_id_snapshot = models.PositiveBigIntegerField(blank=True, null=True)
    package_name_snapshot = models.CharField(max_length=120, blank=True, default='')
    spec_id_snapshot = models.PositiveBigIntegerField(blank=True, null=True)
    spec_name_snapshot = models.CharField(max_length=120, blank=True, default='')
    quantity = models.PositiveSmallIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    sort_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'order_pricing_lines'
        verbose_name = '订单价格明细'
        verbose_name_plural = '订单价格明细'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return f'{self.order.order_no} - {self.source} - {self.amount}'


class DesignatedOrderDraft(models.Model):
    """老板配置指定陪玩时的服务端草稿。

    草稿只保存选择，不保存客户端报价；提交时会重新从静态组合 SKU 计算并校验。
    """

    STATUS_DRAFT = 'draft'
    STATUS_SUBMITTED = 'submitted'
    STATUS_CHOICES = [
        (STATUS_DRAFT, '编辑中'),
        (STATUS_SUBMITTED, '已提交'),
    ]

    boss_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='designated_order_drafts',
    )
    boss_wechat = models.CharField(max_length=50)
    game_id = models.CharField(max_length=100, blank=True, null=True)
    package = models.ForeignKey('catalog.Package', on_delete=models.PROTECT, related_name='designated_order_drafts')
    base_spec = models.ForeignKey('catalog.PackageSpec', on_delete=models.PROTECT, related_name='designated_order_drafts')
    designated_player_ids = models.JSONField(default=list, blank=True)
    booked_hours = models.PositiveSmallIntegerField(default=1)
    boss_note = models.TextField(blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)
    submitted_order = models.OneToOneField(
        Order,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='designated_draft',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'designated_order_drafts'
        verbose_name = '指定陪玩草稿'
        verbose_name_plural = '指定陪玩草稿'
        ordering = ['-updated_at', '-id']

    def __str__(self):
        return f'草稿#{self.pk} - {self.boss_wechat}'


class OrderPlayer(models.Model):
    ROOM_ENTRY_PENDING = 'pending'
    ROOM_ENTRY_CONFIRMED = 'confirmed'
    ROOM_ENTRY_LATE_CONFIRMED = 'late_confirmed'
    ROOM_ENTRY_OVERDUE = 'overdue'
    ROOM_ENTRY_WAIVED = 'waived'
    ROOM_ENTRY_STATUS_CHOICES = [
        (ROOM_ENTRY_PENDING, '等待进入'),
        (ROOM_ENTRY_CONFIRMED, '按时进入'),
        (ROOM_ENTRY_LATE_CONFIRMED, '超时后进入'),
        (ROOM_ENTRY_OVERDUE, '已超时待核实'),
        (ROOM_ENTRY_WAIVED, '管理员免除'),
    ]

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='order_players')
    player = models.ForeignKey('players.Player', on_delete=models.CASCADE, related_name='order_players')
    is_designated = models.BooleanField(default=False)
    designated_type_id = models.IntegerField(blank=True, null=True)
    grab_time = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, default='已接单')
    room_join_deadline = models.DateTimeField(blank=True, null=True, db_index=True, verbose_name='进入房间截止时间')
    room_join_confirmed_at = models.DateTimeField(blank=True, null=True, verbose_name='确认进入房间时间')
    room_join_status = models.CharField(
        max_length=20,
        choices=ROOM_ENTRY_STATUS_CHOICES,
        default=ROOM_ENTRY_PENDING,
        db_index=True,
        verbose_name='进入房间状态',
    )

    class Meta:
        db_table = 'order_players'
        verbose_name = '订单陪玩'
        verbose_name_plural = '订单陪玩列表'
        unique_together = [('order', 'player')]

    def save(self, *args, **kwargs):
        if not self.pk and not self.room_join_deadline:
            self.room_join_deadline = timezone.now() + timedelta(minutes=10)
        super().save(*args, **kwargs)

    def refresh_room_join_status(self, now=None, save=True):
        now = now or timezone.now()
        if (
            self.room_join_status == self.ROOM_ENTRY_PENDING
            and self.room_join_deadline
            and self.room_join_deadline <= now
        ):
            self.room_join_status = self.ROOM_ENTRY_OVERDUE
            if save:
                self.save(update_fields=['room_join_status'])
        return self.room_join_status


class OrderDesignation(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_ACCEPTED = 'accepted'
    STATUS_DECLINED = 'declined'
    STATUS_EXPIRED = 'expired'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_PENDING, '待接受'),
        (STATUS_ACCEPTED, '已接受'),
        (STATUS_DECLINED, '已拒绝'),
        (STATUS_EXPIRED, '已超时'),
        (STATUS_CANCELLED, '已取消'),
    ]

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='designations', verbose_name='订单')
    player = models.ForeignKey('players.Player', on_delete=models.PROTECT, related_name='order_designations', verbose_name='指定陪玩')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    extra_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name='指定服务费')
    invited_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(blank=True, null=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = 'order_designations'
        verbose_name = '指定陪玩邀请'
        verbose_name_plural = '指定陪玩邀请'
        ordering = ['invited_at', 'id']
        constraints = [
            models.UniqueConstraint(fields=['order', 'player'], name='uniq_order_designated_player'),
        ]

    def __str__(self):
        return f'{self.order.order_no} - {self.player.name} - {self.get_status_display()}'


class Rating(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='ratings')
    player = models.ForeignKey('players.Player', on_delete=models.CASCADE, related_name='ratings')
    rating = models.IntegerField()
    comment = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ratings'
        verbose_name = '评分'
        verbose_name_plural = '评分列表'
        unique_together = [('order', 'player')]


class OrderStatusLog(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='status_logs')
    from_status = models.CharField(max_length=20, blank=True, default='')
    to_status = models.CharField(max_length=20)
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True)
    reason = models.CharField(max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'order_status_logs'
        verbose_name = '订单状态日志'
        verbose_name_plural = '订单状态日志列表'
        ordering = ['-created_at']


class OrderEditLog(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='edit_logs')
    admin = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    field_name = models.CharField(max_length=50)
    old_value = models.CharField(max_length=500, blank=True, null=True)
    new_value = models.CharField(max_length=500, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'order_edit_logs'
        verbose_name = '订单编辑日志'
        verbose_name_plural = '订单编辑日志列表'


class CartItem(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='cart_items',
    )
    package = models.ForeignKey(
        'catalog.Package',
        on_delete=models.CASCADE,
        related_name='cart_items',
    )
    spec = models.ForeignKey(
        'catalog.PackageSpec',
        on_delete=models.SET_NULL,
        blank=True, null=True,
        related_name='cart_items',
    )
    spec_id_snapshot = models.CharField(max_length=80, blank=True, default='')
    spec_name = models.CharField(max_length=120, blank=True, default='')
    spec_display_name = models.CharField(max_length=120, blank=True, default='')
    price = models.FloatField()
    quantity = models.PositiveIntegerField(default=1)
    image_url = models.CharField(max_length=500, blank=True, null=True)
    description = models.CharField(max_length=300, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'cart_items'
        verbose_name = '购物车项'
        verbose_name_plural = '购物车列表'
        ordering = ['-updated_at']
        unique_together = [('user', 'package', 'spec_id_snapshot')]

    def __str__(self):
        return f'{self.user.username} - {self.package.name} x{self.quantity}'
