from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from .diamonds import qyuan, yuan_to_diamonds


ZERO = Decimal('0.00')
ALLOWED_RECHARGE_AMOUNTS = tuple(
    Decimal(value) for value in ('10.00', '30.00', '50.00', '100.00', '200.00', '500.00', '1000.00')
)


class ClientWallet(models.Model):
    profile = models.OneToOneField(
        'accounts.ClientProfile',
        on_delete=models.PROTECT,
        related_name='wallet',
        verbose_name='老板',
    )
    # 钱包仍以人民币 Decimal 作为唯一账务真值；客户端按固定 1:10 显示整数钻石。
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, verbose_name='人民币等值余额(元)')
    recharged_total = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, verbose_name='累计充值(元)')
    spent_total = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, verbose_name='累计余额支付(元)')
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

    @property
    def balance_diamonds(self):
        return yuan_to_diamonds(self.balance)

    def __str__(self):
        return f'{self.profile}的钱包'


class RechargeProduct(models.Model):
    """把固定钻石充值档位绑定到微信虚拟支付道具。"""

    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='实际支付金额(元)')
    product_id = models.CharField(
        max_length=20,
        unique=True,
        verbose_name='微信道具ID',
        help_text='例如 recharge_30。必须与微信虚拟支付后台中的道具ID完全一致。',
    )
    goods_price_fen = models.PositiveIntegerField(
        verbose_name='微信道具单价（分）',
        help_text='例如30元填写3000。',
    )
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    sort_order = models.IntegerField(default=0, verbose_name='排序')
    remark = models.CharField(max_length=100, blank=True, default='', verbose_name='备注')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wallet_recharge_products'
        verbose_name = '钻石充值档位'
        verbose_name_plural = '钻石充值档位'
        ordering = ['sort_order', 'id']

    @property
    def diamond_amount(self):
        return yuan_to_diamonds(self.amount)

    def clean(self):
        errors = {}
        amount = qyuan(self.amount)
        if amount not in ALLOWED_RECHARGE_AMOUNTS:
            errors['amount'] = '充值档位仅允许10、30、50、100、200、500、1000元。'
        try:
            yuan_to_diamonds(amount)
        except ValidationError as exc:
            errors['amount'] = exc.message
        if self.goods_price_fen:
            expected_fen = int(amount * Decimal('100'))
            if self.goods_price_fen != expected_fen:
                errors['goods_price_fen'] = (
                    f'当前充值金额为¥{amount:.2f}，'
                    f'微信道具单价应填写{expected_fen}分，而不是{self.goods_price_fen}分。'
                )
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f'{self.product_id} → 💎{self.diamond_amount}（¥{qyuan(self.amount):.2f}）'


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
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='实际支付金额(元)')
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
        verbose_name = '钻石充值单'
        verbose_name_plural = '钻石充值单'
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                check=Q(amount__gt=ZERO),
                name='wallet_recharge_order_amount_positive',
            ),
        ]

    @property
    def diamond_amount(self):
        return yuan_to_diamonds(self.amount)

    def __str__(self):
        return f'{self.recharge_no} - {self.profile} - 💎{self.diamond_amount}'


class ClientWalletLedger(models.Model):
    TYPE_RECHARGE = 'recharge'
    TYPE_ORDER_PAYMENT = 'order_payment'
    TYPE_REFUND_IN = 'refund_in'
    TYPE_ADMIN_ADJUST = 'admin_adjust'

    # 微信直接购买订单在账务上也统一为“人民币购钻石 → 钻石支付订单”。
    # 这两类是内部桥接流水，金额一正一负、用户余额净变化为0，客户端交易页默认隐藏。
    TYPE_VIRTUAL_PURCHASE_CREDIT = 'virtual_purchase_credit'
    TYPE_VIRTUAL_ORDER_PAYMENT = 'virtual_order_payment'

    # 已经退到钱包的钻石退款若再转换为微信原路退款，使用独立流水扣回/恢复，
    # 便于审计且不会与人工调整混淆。
    TYPE_WECHAT_REFUND_CONVERSION = 'wechat_refund_conversion'
    TYPE_WECHAT_REFUND_RESTORE = 'wechat_refund_restore'

    INTERNAL_ENTRY_TYPES = (
        TYPE_VIRTUAL_PURCHASE_CREDIT,
        TYPE_VIRTUAL_ORDER_PAYMENT,
    )

    ENTRY_TYPE_CHOICES = [
        (TYPE_RECHARGE, '充值'),
        (TYPE_ORDER_PAYMENT, '订单支付'),
        (TYPE_REFUND_IN, '退款入账'),
        (TYPE_ADMIN_ADJUST, '人工调整'),
        (TYPE_VIRTUAL_PURCHASE_CREDIT, '微信直付购入钻石（内部）'),
        (TYPE_VIRTUAL_ORDER_PAYMENT, '微信直付订单钻石消费（内部）'),
        (TYPE_WECHAT_REFUND_CONVERSION, '钱包退款转微信原路退款'),
        (TYPE_WECHAT_REFUND_RESTORE, '微信原路退款失败返还钻石'),
    ]

    wallet = models.ForeignKey(ClientWallet, on_delete=models.PROTECT, related_name='ledgers')
    entry_type = models.CharField(max_length=40, choices=ENTRY_TYPE_CHOICES, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='人民币等值变动金额(元)')
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, verbose_name='人民币等值变动后余额(元)')
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

    @property
    def amount_diamonds(self):
        return yuan_to_diamonds(self.amount)

    @property
    def balance_after_diamonds(self):
        return yuan_to_diamonds(self.balance_after)

    def __str__(self):
        return f'{self.wallet.profile} {self.entry_type} {self.amount}'
