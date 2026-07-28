from decimal import Decimal

from django.db import models


ZERO = Decimal('0.00')


class PlayerDiscipline(models.Model):
    player = models.OneToOneField(
        'players.Player',
        on_delete=models.CASCADE,
        related_name='discipline',
        primary_key=True,
        verbose_name='陪玩师',
    )
    suspended_until = models.DateTimeField(blank=True, null=True, db_index=True, verbose_name='暂停接单至')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'player_discipline'
        verbose_name = '陪玩接单限制'
        verbose_name_plural = '陪玩接单限制'

    def __str__(self):
        return f'{self.player.name} - {self.suspended_until or "未暂停"}'


class PlayerCancellationRecord(models.Model):
    STAGE_BEFORE_JOIN = 'before_join'
    STAGE_IN_SERVICE = 'in_service'
    STAGE_CHOICES = [
        (STAGE_BEFORE_JOIN, '进队前取消'),
        (STAGE_IN_SERVICE, '进队后/服务中取消'),
    ]

    REPLACEMENT_PUBLIC = 'public'
    REPLACEMENT_TARGETED = 'targeted'
    REPLACEMENT_CHOICES = [
        (REPLACEMENT_PUBLIC, '公开补位'),
        (REPLACEMENT_TARGETED, '老板重新指定'),
    ]

    order = models.ForeignKey(
        'orders.Order',
        on_delete=models.PROTECT,
        related_name='player_cancellations',
        verbose_name='订单',
    )
    player = models.ForeignKey(
        'players.Player',
        on_delete=models.PROTECT,
        related_name='cancellation_records',
        verbose_name='陪玩师',
    )
    stage = models.CharField(max_length=20, choices=STAGE_CHOICES, db_index=True)
    replacement_mode = models.CharField(max_length=20, choices=REPLACEMENT_CHOICES)
    reason = models.CharField(max_length=500)
    previous_order_status = models.CharField(max_length=20, blank=True, default='')
    was_designated = models.BooleanField(default=False)
    player_type_id_snapshot = models.PositiveBigIntegerField(blank=True, null=True)
    player_type_name_snapshot = models.CharField(max_length=60, blank=True, default='')
    booked_hours_snapshot = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('1.00'))
    used_free_chance = models.BooleanField(default=False)
    fine_rmb = models.DecimalField(max_digits=8, decimal_places=2, default=ZERO)
    fine_fish = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    deducted_fish = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    debt_fish = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    suspended_until = models.DateTimeField(blank=True, null=True, db_index=True)
    wallet_adjustment = models.OneToOneField(
        'earnings.WalletAdjustment',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='player_cancellation',
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'player_cancellation_records'
        verbose_name = '陪玩取消记录'
        verbose_name_plural = '陪玩取消记录'
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f'{self.order.order_no} - {self.player.name} - {self.get_stage_display()}'


class OrderReplacementState(models.Model):
    MODE_PUBLIC = 'public'
    MODE_TARGETED = 'targeted'
    MODE_CHOICES = [
        (MODE_PUBLIC, '公开补位'),
        (MODE_TARGETED, '老板重新指定'),
    ]

    STATUS_OPEN = 'open'
    STATUS_CANCEL_REQUESTED = 'cancel_requested'
    STATUS_RESOLVED = 'resolved'
    STATUS_CHOICES = [
        (STATUS_OPEN, '待处理'),
        (STATUS_CANCEL_REQUESTED, '老板申请取消剩余服务'),
        (STATUS_RESOLVED, '已解决'),
    ]

    order = models.OneToOneField(
        'orders.Order',
        on_delete=models.CASCADE,
        related_name='replacement_state',
        primary_key=True,
        verbose_name='订单',
    )
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, db_index=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_OPEN, db_index=True)
    missing_slots = models.PositiveSmallIntegerField(default=1)
    resume_status = models.CharField(max_length=20, blank=True, default='')
    remaining_minutes = models.PositiveIntegerField(blank=True, null=True)
    cancelled_player_name = models.CharField(max_length=100, blank=True, default='')
    required_player_type_id = models.PositiveBigIntegerField(blank=True, null=True)
    required_player_type_name = models.CharField(max_length=60, blank=True, default='')
    latest_cancellation = models.ForeignKey(
        PlayerCancellationRecord,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='replacement_states',
    )
    resolved_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'order_replacement_states'
        verbose_name = '订单补位状态'
        verbose_name_plural = '订单补位状态'

    def __str__(self):
        return f'{self.order.order_no} - {self.get_mode_display()} - {self.get_status_display()}'
