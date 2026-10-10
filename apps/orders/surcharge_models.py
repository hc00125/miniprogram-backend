"""Independent surcharge storage. No payment, balance or earnings side effects."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from apps.gifts.fields import DiamondPriceField


class OrderSurcharge(models.Model):
    surcharge_no = models.CharField(max_length=64, unique=True)
    order = models.ForeignKey('orders.Order', on_delete=models.PROTECT, related_name='surcharges')
    boss = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    amount_diamonds = DiamondPriceField(validators=[MinValueValidator(1)])
    amount_yuan = models.DecimalField(max_digits=14, decimal_places=2)
    refunded_diamonds = models.PositiveBigIntegerField(default=0)
    # Legacy reference retained for historical compatibility; new payments use FK.
    attempt = models.OneToOneField('wallet.WalletSpendAttempt', on_delete=models.PROTECT, null=True, blank=True)
    allocation_snapshot = models.JSONField(default=dict, blank=True)
    policy_snapshot = models.JSONField(default=dict, blank=True)
    attempt_reference = models.CharField(max_length=100, unique=True, null=True, blank=True)
    status = models.CharField(max_length=24, default='created', choices=[(s, s) for s in
        ('created', 'processing', 'unknown', 'paid', 'failed', 'cancelled', 'partially_refunded', 'refunded')])
    idempotency_key = models.CharField(max_length=100)
    request_digest = models.CharField(max_length=64, validators=[RegexValidator(r'\A[0-9a-f]{64}\Z')])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('boss', 'idempotency_key'), name='surcharge_boss_idempotency'),
            models.CheckConstraint(check=models.Q(amount_diamonds__gt=0, amount_yuan__gt=0), name='surcharge_positive_amount'),
            models.CheckConstraint(check=models.Q(amount_diamonds=models.F('amount_yuan') * 10), name='surcharge_yuan_diamonds'),
            models.CheckConstraint(check=models.Q(refunded_diamonds__lte=models.F('amount_diamonds')), name='surcharge_refund_bound'),
        ]
        ordering = ('-id',)

    def clean(self):
        super().clean()
        if self.order_id and self.order.boss_user_id != self.boss_id:
            raise ValidationError('加价付款人必须为原订单老板')

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class OrderCheckout(models.Model):
    """One confirmation, separate base/surcharge records; never a service order."""
    order = models.OneToOneField('orders.Order', on_delete=models.PROTECT, related_name='checkout')
    boss = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    surcharge = models.OneToOneField(OrderSurcharge, on_delete=models.PROTECT, null=True, blank=True)
    idempotency_key = models.CharField(max_length=100)
    request_digest = models.CharField(max_length=64)
    quote_snapshot = models.JSONField(default=dict)
    base_amount = models.DecimalField(max_digits=12, decimal_places=2)
    surcharge_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    attempt = models.OneToOneField('wallet.WalletSpendAttempt', on_delete=models.PROTECT, null=True, blank=True)
    status = models.CharField(max_length=24, default='created')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('boss', 'idempotency_key'), name='checkout_boss_key'),
            models.CheckConstraint(check=models.Q(base_amount__gte=0, surcharge_amount__gte=0,
                total_amount=models.F('base_amount') + models.F('surcharge_amount')), name='checkout_sum_exact'),
        ]


class OrderCheckoutRefund(models.Model):
    """Full pre-service bundle refund; remote state is NOT local wallet state."""
    checkout = models.OneToOneField(OrderCheckout, on_delete=models.PROTECT, related_name='full_refund')
    base_refund = models.OneToOneField('payments.Refund', on_delete=models.PROTECT)
    refund_no = models.CharField(max_length=64, unique=True)
    surcharge_amount = models.DecimalField(max_digits=12, decimal_places=2)
    source_snapshot = models.JSONField(default=dict)
    request_payload = models.JSONField(default=dict)
    remote_status = models.CharField(max_length=20, default='not_required')
    evidence = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(check=models.Q(surcharge_amount__gt=0),
            name='checkout_refund_positive')]

