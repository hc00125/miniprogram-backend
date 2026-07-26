from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


ZERO = Decimal('0.00')


class ClientWallet(models.Model):
    profile = models.OneToOneField(
        'accounts.ClientProfile',
        on_delete=models.PROTECT,
        related_name='wallet',
        verbose_name='老板',
    )
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, verbose_name='余额(元)')
    recharged_total = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, verbose_name='累计充值(元)')
    spent_total = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, verbose_name='累计余额消费(元)')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'client_wallets'
        verbose_name = '老板钱包'
        verbose_name_plural = '老板钱包'
        constraints = [
            models.CheckConstraint(
                check=Q(balance__gte=ZERO)
                & Q(recharged_total__gte=ZERO)
                & Q(spent_total__gte=ZERO),
                name='client_wallet_balances_non_negative',
            ),
        ]

    def __str__(self):
        return f'{self.profile}的钱包'


class RechargeProduct(models.Model):
    """把充值档位绑定到微信虚拟支付道具。"""

    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='充值金额(元)')
    product_id = models.CharField(
        max_length=20,
        unique=True,
        verbose_name='微信道具ID',
        help_text='例如 recharge_30。必须与微信虚拟支付后台中的道具ID完全一致。',
    )
    goods_price_fen = models.PositiveIntegerField(
        verbose_name='道具单价（分）',
        help_text='例如30元填写3000。',
    )
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    sort_order = models.IntegerField(default=0, verbose_name='排序')
    remark = models.CharField(max_length=100, blank=True, default='', verbose_name='备注')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wallet_recharge_products'
        verbose_name = '充值档位'
        verbose_name_plural = '充值档位'
        ordering = ['sort_order', 'id']

    def clean(self):
        errors = {}
        if self.amount is not None and self.goods_price_fen:
            expected_fen = int(round(float(self.amount) * 100))
            if self.goods_price_fen != expected_fen:
                errors['goods_price_fen'] = (
                    f'当前充值金额为¥{float(self.amount):.2f}，'
                    f'道具单价应填写{expected_fen}分，而不是{self.goods_price_fen}分。'
                )
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f'{self.product_id} → ¥{self.amount}'


class RechargeOrder(models.Model):
    STATUS_CREATED = 'created'
    STATUS_PAYING = 'paying'
    STATUS_PAID = 'paid'
    STATUS_CREDITED = 'credited'
    STATUS_CLOSED = 'closed'
    STATUS_FAILED = 'failed'

    STATUS_CHOICES = [
        (STATUS_CREATED, '已创建'),
        (STATUS_PAYING, '支付中'),
        (STATUS_PAID, '已支付'),
        (STATUS_CREDITED, '已入账'),
        (STATUS_CLOSED, '已关闭'),
        (STATUS_FAILED, '已失败'),
    ]

    CHANNEL_WECHAT_VIRTUAL = 'wechat_virtual'
    CHANNEL_MOCK = 'mock'
    CHANNEL_CHOICES = [
        (CHANNEL_WECHAT_VIRTUAL, '微信虚拟支付'),
        (CHANNEL_MOCK, '模拟支付'),
    ]

    recharge_no = models.CharField(max_length=40, unique=True, db_index=True, verbose_name='充值单号')
    profile = models.ForeignKey(
        'accounts.ClientProfile',
        on_delete=models.PROTECT,
        related_name='recharge_orders',
        verbose_name='老板',
    )
    product = models.ForeignKey(
        RechargeProduct,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name='recharge_orders',
        verbose_name='充值档位',
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='充值金额(元)')
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES, default=CHANNEL_WECHAT_VIRTUAL)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_CREATED, db_index=True)
    third_trade_no = models.CharField(max_length=80, blank=True, default='')
    notify_payload = models.JSONField(blank=True, null=True)
    paid_at = models.DateTimeField(blank=True, null=True)
    credited_at = models.DateTimeField(blank=True, null=True, verbose_name='入账时间')
    expires_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wallet_recharge_orders'
        verbose_name = '充值单'
        verbose_name_plural = '充值单'
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                check=Q(amount__gt=ZERO),
                name='wallet_recharge_order_amount_positive',
            ),
        ]

    def __str__(self):
        return f'{self.recharge_no} - {self.profile} - ¥{self.amount}'


class ClientWalletLedger(models.Model):
    TYPE_RECHARGE = 'recharge'
    TYPE_ORDER_PAYMENT = 'order_payment'
    TYPE_REFUND_IN = 'refund_in'
    TYPE_ADMIN_ADJUST = 'admin_adjust'

    ENTRY_TYPE_CHOICES = [
        (TYPE_RECHARGE, '充值'),
        (TYPE_ORDER_PAYMENT, '订单支付'),
        (TYPE_REFUND_IN, '退款入账'),
        (TYPE_ADMIN_ADJUST, '人工调整'),
    ]

    wallet = models.ForeignKey(ClientWallet, on_delete=models.PROTECT, related_name='ledgers')
    entry_type = models.CharField(max_length=40, choices=ENTRY_TYPE_CHOICES, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='变动金额(元)')
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='变动后余额(元)')
    reference_type = models.CharField(max_length=40, blank=True, default='')
    reference_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='client_wallet_ledger_operations',
    )
    note = models.CharField(max_length=500, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'client_wallet_ledgers'
        verbose_name = '老板钱包流水'
        verbose_name_plural = '老板钱包流水'
        ordering = ['-created_at', '-id']
        constraints = [
            models.CheckConstraint(
                check=~Q(amount=ZERO),
                name='client_wallet_ledger_amount_non_zero',
            ),
            models.CheckConstraint(
                check=Q(balance_after__gte=ZERO),
                name='client_wallet_ledger_balance_after_non_negative',
            ),
            models.UniqueConstraint(
                fields=['wallet', 'entry_type', 'reference_id'],
                condition=~Q(reference_id=''),
                name='uniq_client_wallet_ledger_reference',
            ),
        ]

    def __str__(self):
        return f'{self.wallet.profile} {self.entry_type} {self.amount}'
