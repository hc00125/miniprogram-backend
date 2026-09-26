import uuid
from decimal import Decimal
from django.conf import settings

from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator, MinValueValidator
from .validators import validate_gift_image


from .fields import DiamondPriceField  # Re-export for the initial migration.


def generate_gift_code():
    return 'gift_' + uuid.uuid4().hex


class Gift(models.Model):
    KIND_GIFT = 'gift'
    KIND_INTERNAL_SURCHARGE = 'internal_surcharge'
    kind = models.CharField('用途', max_length=24, default=KIND_GIFT, choices=[(KIND_GIFT, '普通礼物'), (KIND_INTERNAL_SURCHARGE, '内部订单加价配置')])
    internal_enabled = models.BooleanField('内部配置项目启用', default=True)
    name = models.CharField('礼物名称', max_length=80)
    code = models.CharField('稳定代码', max_length=50, unique=True, default=generate_gift_code,
                            validators=[RegexValidator(r'\A[a-z0-9_]+\Z')])
    image = models.ImageField('礼物图片', upload_to='gifts/%Y/%m/', blank=True, validators=[validate_gift_image])
    price_diamonds = DiamondPriceField('钻石单价', validators=[MinValueValidator(0)], null=True, blank=True)
    description = models.CharField('简短描述', max_length=300, blank=True, default='')
    sort_order = models.IntegerField('排序', default=0)
    is_active = models.BooleanField('是否上架', default=False, db_index=True)
    send_enabled = models.BooleanField('允许库存赠送（全局交易仍关闭）', default=False)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'gifts'
        verbose_name = '礼物'
        verbose_name_plural = '礼物目录'
        ordering = ['sort_order', 'id']
        constraints = [models.CheckConstraint(check=(
            models.Q(kind='gift', price_diamonds__isnull=False, price_diamonds__gte=0) & ~models.Q(code='order_surcharge') |
            models.Q(kind='internal_surcharge', code='order_surcharge', price_diamonds__isnull=True, is_active=False, send_enabled=False, image='')
        ), name='gift_kind_catalog_rules')]

    def clean(self):
        super().clean()
        if not self.name.strip():
            raise ValidationError({'name': '名称不能为空'})
        if self.kind == self.KIND_GIFT and self.price_diamonds is None:
            raise ValidationError({'price_diamonds': '普通礼物必须填写非负整数钻价，0表示免费'})
        if self.kind == self.KIND_GIFT and self.is_active and not self.image:
            raise ValidationError({'image': '上架需要图片'})
        if self.pk and Gift.objects.filter(pk=self.pk).exclude(code=self.code).exists():
            raise ValidationError({'code': '稳定代码不可修改'})
        if self.pk and Gift.objects.filter(pk=self.pk).exclude(kind=self.kind).exists():
            raise ValidationError({'kind': '用途创建后不可修改'})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    def delete(self, *args, **kwargs):
        if self.kind == self.KIND_INTERNAL_SURCHARGE:
            raise ValidationError('内部配置项目不可删除，请停用')
        return super().delete(*args, **kwargs)


class PlayerGiftConfig(models.Model):
    player = models.ForeignKey('players.Player', on_delete=models.PROTECT)
    gift = models.ForeignKey(Gift, on_delete=models.PROTECT)
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('25.00'))
    is_enabled = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('player', 'gift'), name='gift_unique_player_config'),
            models.CheckConstraint(check=models.Q(commission_rate__gte=0, commission_rate__lte=100), name='gift_valid_commission'),
            models.CheckConstraint(check=models.Q(version__gte=1), name='gift_config_positive_version'),
        ]

    def save(self, *args, _audited=False, **kwargs):
        if not _audited:
            raise ValidationError('必须通过带权限和审计的配置服务保存')
        if isinstance(self.commission_rate, (bool, float)):
            raise ValidationError('抽成必须使用Decimal，不允许float或bool')
        self.full_clean()
        return super().save(*args, **kwargs)


class PlayerGiftConfigAudit(models.Model):
    config = models.ForeignKey(PlayerGiftConfig, on_delete=models.PROTECT, related_name='audits')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.CharField(max_length=300)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)


class FoundationRecord(models.Model):
    """Storage foundation only; does not execute any monetary operation."""
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class GiftPurchase(FoundationRecord):
    purchase_no = models.CharField(max_length=64, unique=True)
    attempt = models.OneToOneField('wallet.WalletSpendAttempt', on_delete=models.PROTECT, null=True, blank=True)
    buyer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    gift = models.ForeignKey(Gift, on_delete=models.PROTECT)
    mode = models.CharField(max_length=16, choices=[('inventory', '入库'), ('direct', '直送')], default='inventory')
    recipient = models.ForeignKey('players.Player', on_delete=models.PROTECT, null=True, blank=True)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    snapshot = models.JSONField(default=dict)
    idempotency_key = models.CharField(max_length=100)
    request_digest = models.CharField(max_length=64, validators=[RegexValidator(r'\A[0-9a-f]{64}\Z')])
    status = models.CharField(max_length=24, default='created', choices=[(s, s) for s in
        ('created', 'processing', 'unknown', 'paid', 'failed', 'cancelled', 'partially_refunded', 'refunded')])

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('buyer', 'idempotency_key'), name='gift_purchase_idempotency'),
            models.CheckConstraint(check=models.Q(quantity__gt=0), name='gift_purchase_quantity'),
            models.CheckConstraint(check=(models.Q(mode='inventory', recipient__isnull=True) | models.Q(mode='direct', recipient__isnull=False)), name='gift_purchase_recipient_mode'),
        ]

    def clean(self):
        super().clean()
        if self.mode == 'inventory' and isinstance(self.snapshot, dict) and any(
                key in self.snapshot for key in ('commission_rate', 'config_id', 'config_version', 'recipient_id')):
            raise ValidationError('入库购买不能冻结收礼人抽成')
        if self.gift_id and self.gift.kind != Gift.KIND_GIFT:
            raise ValidationError('内部配置项目不可购买')


class GiftInventoryLot(FoundationRecord):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    gift = models.ForeignKey(Gift, on_delete=models.PROTECT)
    source = models.CharField(max_length=16, choices=[('paid', '购买'), ('admin_free', '后台免费')])
    source_reference = models.CharField(max_length=100, unique=True)
    purchase = models.OneToOneField(GiftPurchase, on_delete=models.PROTECT, null=True, blank=True)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    remaining = models.PositiveIntegerField()
    reserved = models.PositiveIntegerField(default=0)
    cost_diamonds = models.PositiveBigIntegerField(default=0)
    earnings_eligible = models.BooleanField(default=False)
    snapshot = models.JSONField(default=dict)
    status = models.CharField(max_length=16, default='active', choices=[('active', '正常'), ('frozen', '冻结'), ('revoked', '撤销')])

    class Meta:
        permissions = [('grant_free_gift', '允许显式免费零收益发放礼物')]
        constraints = [
            models.CheckConstraint(check=models.Q(quantity__gt=0, remaining__lte=models.F('quantity'), reserved__lte=models.F('remaining')), name='gift_lot_quantity_bounds'),
            models.CheckConstraint(check=(models.Q(source='paid', purchase__isnull=False) | models.Q(source='admin_free', purchase__isnull=True, cost_diamonds=0, earnings_eligible=False)), name='gift_lot_source_rules'),
        ]

    def clean(self):
        super().clean()
        if self.purchase_id and (self.purchase.mode != 'inventory' or self.purchase.buyer_id != self.owner_id or self.purchase.gift_id != self.gift_id):
            raise ValidationError('库存来源购买与拥有者或礼物不匹配')
        if self.gift_id and self.gift.kind != Gift.KIND_GIFT:
            raise ValidationError('内部配置项目不可入库或发放')


class GiftInventoryLedger(FoundationRecord):
    lot = models.ForeignKey(GiftInventoryLot, on_delete=models.PROTECT, related_name='ledger')
    event_key = models.CharField(max_length=120, unique=True)
    kind = models.CharField(max_length=16, choices=[(s, s) for s in ('grant', 'purchase', 'send', 'refund', 'revoke')])
    quantity_delta = models.IntegerField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.CharField(max_length=300)

    class Meta:
        constraints = [models.CheckConstraint(check=~models.Q(quantity_delta=0), name='gift_ledger_nonzero')]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('库存流水不可改写')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('库存流水不可删除')


class GiftTransfer(FoundationRecord):
    transfer_no = models.CharField(max_length=64, unique=True)
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    recipient = models.ForeignKey('players.Player', on_delete=models.PROTECT)
    gift = models.ForeignKey(Gift, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    source = models.CharField(max_length=16, default='inventory', choices=[('inventory', '库存'), ('direct', '直送')])
    purchase = models.OneToOneField(GiftPurchase, on_delete=models.PROTECT, null=True, blank=True)
    idempotency_key = models.CharField(max_length=100)
    request_digest = models.CharField(max_length=64, validators=[RegexValidator(r'\A[0-9a-f]{64}\Z')])
    commission_snapshot = models.JSONField()
    status = models.CharField(max_length=16, default='created', choices=[(s, s) for s in ('created', 'delivered', 'failed', 'reversed')])

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('sender', 'idempotency_key'), name='gift_transfer_idempotency'),
            models.CheckConstraint(check=models.Q(quantity__gt=0), name='gift_transfer_quantity'),
            models.CheckConstraint(check=(models.Q(source='direct', purchase__isnull=False) | models.Q(source='inventory', purchase__isnull=True)), name='gift_transfer_source'),
        ]

    def clean(self):
        super().clean()
        if self.recipient_id and self.recipient.user_id == self.sender_id:
            raise ValidationError('不允许自赠')
        if self.gift_id and self.gift.kind != Gift.KIND_GIFT:
            raise ValidationError('内部配置项目不可赠送')
        if self.purchase_id and (self.purchase.mode != 'direct' or self.purchase.buyer_id != self.sender_id or
                                 self.purchase.recipient_id != self.recipient_id or self.purchase.gift_id != self.gift_id):
            raise ValidationError('直送来源不匹配')


class GiftTransferAllocation(FoundationRecord):
    transfer = models.ForeignKey(GiftTransfer, on_delete=models.PROTECT, related_name='allocations')
    lot = models.ForeignKey(GiftInventoryLot, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    snapshot = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('transfer', 'lot'), name='gift_transfer_unique_lot'),
            models.CheckConstraint(check=models.Q(quantity__gt=0), name='gift_allocation_quantity'),
        ]

    def clean(self):
        super().clean()
        if self.transfer_id and self.lot_id and (self.transfer.source != 'inventory' or
                self.transfer.sender_id != self.lot.owner_id or self.transfer.gift_id != self.lot.gift_id or
                self.quantity > self.lot.quantity or self.quantity > self.transfer.quantity):
            raise ValidationError('库存分配的来源、拥有者、礼物或数量不匹配')


class GiftEarning(FoundationRecord):
    transfer = models.ForeignKey(GiftTransfer, on_delete=models.PROTECT)
    allocation = models.OneToOneField(GiftTransferAllocation, on_delete=models.PROTECT, null=True, blank=True)
    player = models.ForeignKey('players.Player', on_delete=models.PROTECT)
    earnings_eligible = models.BooleanField(default=False)
    gross_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    commission_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    available_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    withdrawing_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    withdrawn_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    debt_offset_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    reversed_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    snapshot = models.JSONField()
    release_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, default='frozen', choices=[(s, s) for s in ('pending', 'frozen', 'available', 'reversed')])

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('transfer',), condition=models.Q(allocation__isnull=True), name='gift_unique_direct_earning'),
            models.CheckConstraint(check=models.Q(gross_amount__gte=0, commission_amount__gte=0, net_amount__gte=0,
                available_amount__gte=0, withdrawing_amount__gte=0, withdrawn_amount__gte=0, debt_offset_amount__gte=0, reversed_amount__gte=0), name='gift_earning_nonnegative'),
            models.CheckConstraint(check=models.Q(gross_amount=models.F('commission_amount') + models.F('net_amount')), name='gift_earning_conservation'),
            models.CheckConstraint(check=models.Q(net_amount__gte=models.F('available_amount') + models.F('withdrawing_amount') + models.F('withdrawn_amount') + models.F('debt_offset_amount') + models.F('reversed_amount')), name='gift_earning_bucket_bounds'),
            models.CheckConstraint(check=models.Q(earnings_eligible=True) | models.Q(gross_amount=0, net_amount=0, commission_amount=0), name='gift_free_zero_earning'),
        ]

    def clean(self):
        super().clean()
        if self.transfer_id:
            if self.transfer.status != 'delivered' or self.transfer.recipient_id != self.player_id:
                raise ValidationError('收益仅可关联已送达且收礼人一致的赠送')
            if (self.transfer.source == 'inventory') != bool(self.allocation_id):
                raise ValidationError('库存收益必须有批次分配，直送收益不得有库存分配')
        if self.allocation_id and (self.allocation.transfer_id != self.transfer_id or
                self.earnings_eligible != self.allocation.lot.earnings_eligible):
            raise ValidationError('收益分配或收益资格与原批次不符')


class GiftRefund(FoundationRecord):
    refund_no = models.CharField(max_length=64, unique=True)
    purchase = models.ForeignKey(GiftPurchase, on_delete=models.PROTECT)
    transfer = models.ForeignKey(GiftTransfer, on_delete=models.PROTECT, null=True, blank=True)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    diamonds = models.PositiveBigIntegerField()
    amount_yuan = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.CharField(max_length=300)
    status = models.CharField(max_length=16, default='draft', choices=[(s, s) for s in ('draft', 'approved', 'processing', 'unknown', 'completed', 'rejected', 'manual')])
    wallet_status = models.CharField(max_length=16, default='blocked')
    coin_status = models.CharField(max_length=16, default='blocked')
    inventory_status = models.CharField(max_length=16, default='blocked')
    earning_status = models.CharField(max_length=16, default='blocked')

    class Meta:
        constraints = [
            models.CheckConstraint(check=models.Q(quantity__gt=0), name='gift_refund_quantity'),
            models.CheckConstraint(check=models.Q(amount_yuan__isnull=True) | models.Q(amount_yuan__gte=0), name='gift_refund_yuan_nonnegative'),
        ]

    def clean(self):
        super().clean()
        if self.purchase_id and self.quantity > self.purchase.quantity:
            raise ValidationError('退款数量超过原购买')
        if self.transfer_id and self.transfer.purchase_id != self.purchase_id:
            raise ValidationError('退款直送来源不匹配')
