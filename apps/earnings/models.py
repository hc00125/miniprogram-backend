from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


ZERO = Decimal('0.00')


class EarningsConfig(models.Model):
    key = models.CharField(max_length=30, unique=True, default='default')
    default_commission_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('15.00'),
        validators=[MinValueValidator(ZERO), MaxValueValidator(Decimal('100.00'))],
        verbose_name='默认抽成比例(%)',
    )
    review_days = models.PositiveSmallIntegerField(default=8, verbose_name='工资审核天数')
    min_withdrawal_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('1.00'),
        validators=[MinValueValidator(Decimal('0.01'))],
        verbose_name='最低提现金额',
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='updated_earnings_configs',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'earnings_config'
        verbose_name = '收益配置'
        verbose_name_plural = '收益配置'

    def __str__(self):
        return f'默认抽成 {self.default_commission_rate}% / 审核 {self.review_days} 天'


class OrderCommissionOverride(models.Model):
    order = models.OneToOneField(
        'orders.Order',
        on_delete=models.CASCADE,
        related_name='commission_override',
        verbose_name='订单',
    )
    commission_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(ZERO), MaxValueValidator(Decimal('100.00'))],
        verbose_name='订单抽成比例(%)',
    )
    reason = models.CharField(max_length=300, verbose_name='调整原因')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='created_commission_overrides',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'order_commission_overrides'
        verbose_name = '订单抽成设置'
        verbose_name_plural = '订单抽成设置'

    def __str__(self):
        return f'{self.order.order_no} - {self.commission_rate}%'


class PlayerWallet(models.Model):
    player = models.OneToOneField(
        'players.Player',
        on_delete=models.CASCADE,
        related_name='wallet',
        verbose_name='陪玩师',
    )
    pending_balance = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, verbose_name='审核中鱼干')
    available_balance = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, verbose_name='可提现鱼干')
    withdrawing_balance = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, verbose_name='提现中鱼干')
    withdrawn_total = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, verbose_name='累计已提现')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'player_wallets'
        verbose_name = '陪玩钱包'
        verbose_name_plural = '陪玩钱包'

    def __str__(self):
        return f'{self.player.name}的钱包'


class PlayerEarning(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_AVAILABLE = 'available'
    STATUS_FROZEN = 'frozen'
    STATUS_REVERSED = 'reversed'
    STATUS_CHOICES = [
        (STATUS_PENDING, '审核中'),
        (STATUS_AVAILABLE, '已审核'),
        (STATUS_FROZEN, '已冻结'),
        (STATUS_REVERSED, '已冲销'),
    ]

    order = models.ForeignKey(
        'orders.Order',
        on_delete=models.PROTECT,
        related_name='player_earnings',
        verbose_name='原订单',
    )
    player = models.ForeignKey(
        'players.Player',
        on_delete=models.PROTECT,
        related_name='earnings',
        verbose_name='陪玩师',
    )
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='分配收入')
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, verbose_name='抽成比例(%)')
    commission_amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='平台抽成')
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='应得鱼干')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    review_until = models.DateTimeField(db_index=True, verbose_name='预计审核完成时间')
    available_at = models.DateTimeField(blank=True, null=True, verbose_name='转为可提现时间')
    available_amount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, verbose_name='当前可提现')
    withdrawing_amount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, verbose_name='当前提现中')
    withdrawn_amount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, verbose_name='累计已提现')
    freeze_reason = models.CharField(max_length=300, blank=True, default='', verbose_name='冻结原因')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'player_earnings'
        verbose_name = '订单工资'
        verbose_name_plural = '订单工资'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['order', 'player'], name='uniq_order_player_earning'),
            models.CheckConstraint(
                condition=models.Q(gross_amount__gte=ZERO)
                & models.Q(commission_amount__gte=ZERO)
                & models.Q(net_amount__gte=ZERO)
                & models.Q(available_amount__gte=ZERO)
                & models.Q(withdrawing_amount__gte=ZERO)
                & models.Q(withdrawn_amount__gte=ZERO),
                name='earning_amounts_non_negative',
            ),
        ]

    def __str__(self):
        return f'{self.order.order_no} - {self.player.name} - {self.net_amount}'


class Withdrawal(models.Model):
    STATUS_PENDING_REVIEW = 'pending_review'
    STATUS_PENDING_PAYMENT = 'pending_payment'
    STATUS_PAID = 'paid'
    STATUS_REJECTED = 'rejected'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING_REVIEW, '待审核'),
        (STATUS_PENDING_PAYMENT, '待打款'),
        (STATUS_PAID, '已打款'),
        (STATUS_REJECTED, '已驳回'),
        (STATUS_FAILED, '打款失败'),
    ]

    METHOD_WECHAT = 'wechat'
    METHOD_ALIPAY = 'alipay'
    METHOD_BANK = 'bank'
    METHOD_OTHER = 'other'
    METHOD_CHOICES = [
        (METHOD_WECHAT, '微信'),
        (METHOD_ALIPAY, '支付宝'),
        (METHOD_BANK, '银行卡'),
        (METHOD_OTHER, '其他'),
    ]

    withdrawal_no = models.CharField(max_length=32, unique=True, db_index=True)
    player = models.ForeignKey('players.Player', on_delete=models.PROTECT, related_name='withdrawals')
    wallet = models.ForeignKey(PlayerWallet, on_delete=models.PROTECT, related_name='withdrawals')
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_PENDING_REVIEW, db_index=True)
    payment_method = models.CharField(max_length=20, choices=METHOD_CHOICES, default=METHOD_WECHAT)
    account_name = models.CharField(max_length=80, verbose_name='收款人')
    account_no = models.CharField(max_length=150, verbose_name='收款账号')
    request_note = models.CharField(max_length=300, blank=True, default='')
    reject_reason = models.CharField(max_length=300, blank=True, default='')
    admin_note = models.CharField(max_length=500, blank=True, default='')
    transfer_no = models.CharField(max_length=150, blank=True, default='', verbose_name='转账流水号')
    proof_url = models.CharField(max_length=500, blank=True, default='', verbose_name='打款凭证URL')
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='reviewed_withdrawals',
    )
    reviewed_at = models.DateTimeField(blank=True, null=True)
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='paid_withdrawals',
    )
    paid_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'withdrawals'
        verbose_name = '提现申请'
        verbose_name_plural = '提现申请'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.withdrawal_no} - {self.player.name} - {self.amount}'


class WithdrawalAllocation(models.Model):
    withdrawal = models.ForeignKey(Withdrawal, on_delete=models.CASCADE, related_name='allocations')
    earning = models.ForeignKey(PlayerEarning, on_delete=models.PROTECT, related_name='withdrawal_allocations')
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'withdrawal_allocations'
        verbose_name = '提现工资分配'
        verbose_name_plural = '提现工资分配'
        constraints = [
            models.UniqueConstraint(fields=['withdrawal', 'earning'], name='uniq_withdrawal_earning_allocation'),
        ]


class WalletLedger(models.Model):
    BUCKET_PENDING = 'pending'
    BUCKET_AVAILABLE = 'available'
    BUCKET_WITHDRAWING = 'withdrawing'
    BUCKET_WITHDRAWN = 'withdrawn'
    BUCKET_CHOICES = [
        (BUCKET_PENDING, '审核中'),
        (BUCKET_AVAILABLE, '可提现'),
        (BUCKET_WITHDRAWING, '提现中'),
        (BUCKET_WITHDRAWN, '已提现'),
    ]

    TYPE_EARNING_CREATED = 'earning_created'
    TYPE_EARNING_RELEASED = 'earning_released'
    TYPE_EARNING_ADJUSTED = 'earning_adjusted'
    TYPE_EARNING_REVERSED = 'earning_reversed'
    TYPE_WITHDRAWAL_FROZEN = 'withdrawal_frozen'
    TYPE_WITHDRAWAL_REFUNDED = 'withdrawal_refunded'
    TYPE_WITHDRAWAL_PAID = 'withdrawal_paid'
    TYPE_ADMIN_ADJUSTMENT = 'admin_adjustment'
    ENTRY_TYPE_CHOICES = [
        (TYPE_EARNING_CREATED, '工资进入审核'),
        (TYPE_EARNING_RELEASED, '工资审核通过'),
        (TYPE_EARNING_ADJUSTED, '工资金额调整'),
        (TYPE_EARNING_REVERSED, '工资冲销'),
        (TYPE_WITHDRAWAL_FROZEN, '提现冻结'),
        (TYPE_WITHDRAWAL_REFUNDED, '提现退回'),
        (TYPE_WITHDRAWAL_PAID, '提现完成'),
        (TYPE_ADMIN_ADJUSTMENT, '管理员调整'),
    ]

    wallet = models.ForeignKey(PlayerWallet, on_delete=models.PROTECT, related_name='ledger_entries')
    entry_type = models.CharField(max_length=40, choices=ENTRY_TYPE_CHOICES, db_index=True)
    bucket = models.CharField(max_length=20, choices=BUCKET_CHOICES)
    delta = models.DecimalField(max_digits=12, decimal_places=2)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    reference_type = models.CharField(max_length=40, blank=True, default='')
    reference_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    note = models.CharField(max_length=500, blank=True, default='')
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='wallet_ledger_operations',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'wallet_ledger'
        verbose_name = '钱包流水'
        verbose_name_plural = '钱包流水'
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f'{self.wallet.player.name} {self.entry_type} {self.delta}'
