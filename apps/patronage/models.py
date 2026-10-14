import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.db.models import Q
from django.db.models.functions import Floor

from .pricing import PACKAGE_CHOICES


def validate_hourly_step(value):
    if value is not None and (not value.is_finite() or value % Decimal('0.5')):
        raise ValidationError('包天时薪须按0.5元步长填写，不会自动舍入。')


def purchase_number():
    return 'PN' + uuid.uuid4().hex.upper()


class PatronageSettings(models.Model):
    """One global RMB price list; reads use unsaved defaults when absent."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled = models.BooleanField(default=True, verbose_name='目录配置启用')
    purchase_enabled = models.BooleanField(default=False, verbose_name='交易业务开关（还需运行时总闸）')
    commission_rate = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal('0.25'),
        validators=[MinValueValidator(0), MaxValueValidator(1)], verbose_name='统一平台抽成比例',
        help_text='0.25 = 25%；允许0。仅冠名与包天，不影响其他业务。')
    day_price_yuan = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('188.00'), validators=[MinValueValidator(Decimal('0.10'))], verbose_name='日冠价格（元）')
    week_price_yuan = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('520.00'), validators=[MinValueValidator(Decimal('0.10'))], verbose_name='周冠价格（元）')
    month_price_yuan = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('1314.00'), validators=[MinValueValidator(Decimal('0.10'))], verbose_name='月冠价格（元）')
    quarter_price_yuan = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('2888.00'), validators=[MinValueValidator(Decimal('0.10'))], verbose_name='季冠价格（元）')
    year_price_yuan = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('9999.00'), validators=[MinValueValidator(Decimal('0.10'))], verbose_name='年冠价格（元）')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '冠名/包天统一配置'
        verbose_name_plural = verbose_name
        constraints = [
            models.CheckConstraint(check=Q(id=1), name='patronage_config_singleton'),
            models.CheckConstraint(check=Q(commission_rate__gte=0, commission_rate__lte=1), name='patronage_commission_range'),
            *[models.CheckConstraint(check=Q(**{f'{code}_price_yuan__gt': 0}), name=f'patronage_{code}_positive') for code in ('day', 'week', 'month', 'quarter', 'year')],
        ]

    def __str__(self):
        return '冠名/包天统一配置（人民币）'


class PlayerPatronageConfig(models.Model):
    player = models.OneToOneField('players.Player', on_delete=models.PROTECT, related_name='patronage_config', verbose_name='陪玩')
    hourly_rate_yuan = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal('0.50')), validate_hourly_step], verbose_name='包天计价时薪（元）',
        help_text='按0.5元步长填写。不绑定商品。包天 = 时薪 × 8 × 0.85；为空时不开包天。计价不是履约计时。')
    enabled = models.BooleanField(default=True, verbose_name='包天配置启用')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '陪玩包天时薪'
        verbose_name_plural = verbose_name
        constraints = [
            models.CheckConstraint(check=Q(hourly_rate_yuan__isnull=True) | Q(hourly_rate_yuan__gt=0), name='patronage_hourly_positive'),
            models.CheckConstraint(check=Q(hourly_rate_yuan__isnull=True)
                | Q(hourly_rate_yuan=Floor(models.F('hourly_rate_yuan') * 2) / Decimal('2')),
                name='patronage_hourly_half_yuan'),
        ]

    def __str__(self):
        return f'{self.player.name} 包天计价'


class PlayerNamingServiceConfig(models.Model):
    player = models.ForeignKey('players.Player', on_delete=models.PROTECT, related_name='naming_service_configs')
    package_code = models.CharField(max_length=16, choices=PACKAGE_CHOICES[:-1])
    service_hours = models.DecimalField(max_digits=12, decimal_places=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('player', 'package_code'), name='naming_service_player_package'),
            models.CheckConstraint(check=Q(package_code__in=['day', 'week', 'month', 'quarter', 'year']), name='naming_service_package'),
            models.CheckConstraint(check=Q(service_hours__gt=0), name='naming_service_positive'),
            models.CheckConstraint(check=Q(service_hours=Floor(models.F('service_hours') * 2) / Decimal('2')), name='naming_service_half_hour'),
        ]


class PatronagePurchase(models.Model):
    """Immutable purchase snapshots, NOT an earnings ledger or service order."""
    STATUS_CHOICES = [(s, s) for s in ('created', 'processing', 'unknown', 'paid', 'failed')]
    purchase_no = models.CharField(max_length=40, default=purchase_number, unique=True, editable=False)
    boss = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='patronage_purchases')
    player = models.ForeignKey('players.Player', on_delete=models.PROTECT, related_name='patronage_purchases')
    player_name = models.CharField(max_length=50)
    package_code = models.CharField(max_length=16, choices=PACKAGE_CHOICES)
    package_name = models.CharField(max_length=40)
    amount_yuan = models.DecimalField(max_digits=12, decimal_places=2)
    commission_rate = models.DecimalField(max_digits=5, decimal_places=4)
    # Preserve exact split, without inventing a settlement rounding policy.
    platform_amount_yuan = models.DecimalField(max_digits=16, decimal_places=6)
    player_amount_yuan = models.DecimalField(max_digits=16, decimal_places=6)
    price_version = models.CharField(max_length=64)
    config_snapshot = models.JSONField()
    idempotency_key = models.CharField(max_length=100)
    attempt = models.OneToOneField('wallet.WalletSpendAttempt', on_delete=models.PROTECT,
        null=True, blank=True, related_name='patronage_purchase')
    payment_status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='created', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    bonus_naming_days = models.PositiveSmallIntegerField(default=0)
    blockers = models.JSONField(default=list, blank=True)
    IMMUTABLE = ('purchase_no', 'boss_id', 'player_id', 'player_name', 'package_code', 'package_name',
                 'amount_yuan', 'commission_rate', 'platform_amount_yuan', 'player_amount_yuan',
                 'price_version', 'config_snapshot', 'idempotency_key', 'bonus_naming_days')

    class Meta:
        verbose_name = '冠名/包天购买记录（只读）'
        verbose_name_plural = verbose_name
        ordering = ('-created_at', '-id')
        constraints = [
            models.UniqueConstraint(fields=('boss', 'idempotency_key'), name='patronage_boss_idempotency'),
            models.CheckConstraint(check=Q(payment_status__in=['created', 'processing', 'unknown', 'paid', 'failed']), name='patronage_payment_state'),
            models.CheckConstraint(check=Q(amount_yuan__gt=0, commission_rate__gte=0, commission_rate__lte=1), name='patronage_purchase_amount_rate'),
            models.CheckConstraint(check=Q(platform_amount_yuan=models.F('amount_yuan') * models.F('commission_rate')) & Q(player_amount_yuan=models.F('amount_yuan') - models.F('platform_amount_yuan')), name='patronage_exact_split'),
            models.CheckConstraint(check=~Q(payment_status='paid') | Q(paid_at__isnull=False), name='patronage_paid_timestamp'),
            models.CheckConstraint(check=(Q(starts_at__isnull=True, expires_at__isnull=True) | Q(starts_at__isnull=False, expires_at__isnull=False, expires_at__gt=models.F('starts_at'))), name='patronage_purchase_interval'),
            models.CheckConstraint(check=Q(package_code='day_pass', bonus_naming_days=7) | Q(package_code__in=['day', 'week', 'month', 'quarter', 'year'], bonus_naming_days=0), name='patronage_bonus_consistency'),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            old = type(self).objects.get(pk=self.pk)
            if any(getattr(old, field) != getattr(self, field) for field in self.IMMUTABLE):
                raise ValidationError('购买价格和配置快照不可修改')
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.purchase_no


class CrownGrant(models.Model):
    """Non-exclusive display right; no scheduling, lock, timer or earnings."""
    purchase = models.ForeignKey(PatronagePurchase, on_delete=models.PROTECT, related_name='crowns')
    source = models.CharField(max_length=20, choices=[('purchase', '购买冠名'), ('day_pass_bonus', '包天赠送')])
    package_name = models.CharField(max_length=40)
    starts_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    # Legacy grants have no extension metadata; never rewrite their intervals.
    extension_starts_at = models.DateTimeField(null=True, blank=True)
    duration_days = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = '冠名展示权益（只读）'
        verbose_name_plural = verbose_name
        ordering = ('starts_at', 'id')
        constraints = [
            models.UniqueConstraint(fields=('purchase', 'source'), name='patronage_grant_source_once'),
            models.CheckConstraint(check=Q(expires_at__gt=models.F('starts_at')), name='patronage_grant_interval'),
            models.CheckConstraint(check=Q(source__in=['purchase', 'day_pass_bonus']), name='patronage_grant_source'),
        ]
        indexes = [models.Index(fields=('starts_at', 'expires_at'), name='patronage_grant_window')]

    def __str__(self):
        return f'{self.purchase.purchase_no}: {self.package_name}'


class PatronageEarning(models.Model):
    """One immutable income receipt per purchase, not a withdrawable sub-wallet.

    The purchase primary key is also this receipt's identity, allowing ledger
    references to be written before the final receipt within the same atomic block.
    """
    purchase = models.OneToOneField(PatronagePurchase, primary_key=True,
        on_delete=models.PROTECT, related_name='earning')
    player = models.ForeignKey('players.Player', on_delete=models.PROTECT,
        related_name='patronage_earnings')
    payment_ledger = models.OneToOneField('wallet.ClientWalletLedger', on_delete=models.PROTECT)
    amount_yuan = models.DecimalField(max_digits=12, decimal_places=2)
    commission_rate = models.DecimalField(max_digits=5, decimal_places=4)
    platform_amount_yuan = models.DecimalField(max_digits=16, decimal_places=6)
    player_amount_yuan = models.DecimalField(max_digits=16, decimal_places=6)
    fish_per_yuan = models.DecimalField(max_digits=24, decimal_places=12)
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2)
    commission_amount = models.DecimalField(max_digits=12, decimal_places=2)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2)
    debt_offset_amount = models.DecimalField(max_digits=12, decimal_places=2)
    credited_amount = models.DecimalField(max_digits=12, decimal_places=2)
    debt_ledger = models.OneToOneField('earnings.WalletLedger', on_delete=models.PROTECT,
        null=True, blank=True, related_name='patronage_debt_income')
    credit_ledger = models.OneToOneField('earnings.WalletLedger', on_delete=models.PROTECT,
        null=True, blank=True, related_name='patronage_available_income')
    snapshot = models.JSONField()
    release_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.CheckConstraint(check=Q(amount_yuan__gt=0, fish_per_yuan__gt=0,
                commission_rate__gte=0, commission_rate__lte=1, gross_amount__gte=0,
                commission_amount__gte=0, net_amount__gte=0, debt_offset_amount__gte=0,
                credited_amount__gte=0), name='patronage_income_positive'),
            models.CheckConstraint(check=Q(gross_amount=models.F('commission_amount') + models.F('net_amount'))
                & Q(net_amount=models.F('debt_offset_amount') + models.F('credited_amount')),
                name='patronage_income_conserved'),
            models.CheckConstraint(check=Q(platform_amount_yuan=models.F('amount_yuan') * models.F('commission_rate'))
                & Q(player_amount_yuan=models.F('amount_yuan') - models.F('platform_amount_yuan')),
                name='patronage_income_yuan_split'),
            models.CheckConstraint(check=(Q(debt_offset_amount=0, debt_ledger__isnull=True)
                | Q(debt_offset_amount__gt=0, debt_ledger__isnull=False))
                & (Q(credited_amount=0, credit_ledger__isnull=True)
                | Q(credited_amount__gt=0, credit_ledger__isnull=False)),
                name='patronage_income_ledger_presence'),
        ]
