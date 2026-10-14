"""Durable shared wallet spends. No user/session secrets are persisted."""
import uuid
from django.core.exceptions import ValidationError
from django.db import models


def external_spend_id():
    return 'WS' + uuid.uuid4().hex[:28].upper()


class WalletSpendAttempt(models.Model):
    ACTIVE = ('prepared', 'dispatching', 'unknown', 'succeeded')
    wallet = models.ForeignKey('wallet.ClientWallet', on_delete=models.PROTECT)
    kind = models.CharField(max_length=24)
    business_no = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=100)
    request_digest = models.CharField(max_length=64)
    external_id = models.CharField(max_length=32, unique=True, default=external_spend_id)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reserved_amount = models.DecimalField(max_digits=12, decimal_places=2)
    source_snapshot = models.JSONField(default=dict)
    request_payload = models.JSONField(default=dict)
    status = models.CharField(max_length=16, default='prepared', db_index=True)
    evidence = models.JSONField(default=dict, blank=True)
    blocker = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    IMMUTABLE = ('wallet_id', 'kind', 'business_no', 'idempotency_key', 'request_digest',
                 'external_id', 'amount', 'source_snapshot', 'request_payload')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['wallet', 'kind', 'idempotency_key'], name='wallet_spend_key'),
            models.UniqueConstraint(fields=['kind', 'business_no'], name='wallet_spend_business'),
            # Conservative serialization also reserves coin provenance, not only value.
            models.UniqueConstraint(fields=['wallet'], condition=models.Q(status__in=('prepared', 'dispatching', 'unknown', 'succeeded')), name='wallet_one_active_spend'),
            models.CheckConstraint(check=models.Q(amount__gt=0), name='wallet_spend_positive'),
            models.CheckConstraint(check=(models.Q(status__in=('prepared', 'dispatching', 'unknown', 'succeeded'), reserved_amount=models.F('amount')) | models.Q(status__in=('completed', 'failed'), reserved_amount=0)), name='wallet_spend_reservation_state'),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            if any(getattr(old, f) != getattr(self, f) for f in self.IMMUTABLE):
                raise ValidationError('Spend request and source are immutable')
        return super().save(*args, **kwargs)


class WalletSpendAudit(models.Model):
    attempt = models.ForeignKey(WalletSpendAttempt, on_delete=models.PROTECT, related_name='audit_events')
    event = models.CharField(max_length=40)
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class OrderWalletSpend(models.Model):
    """Order identity, not a retry key, owns one spend. Null + blocker means
    historical uncertainty, never a failed spend or permission to re-debit.
    """
    order = models.OneToOneField('orders.Order', on_delete=models.PROTECT, related_name='wallet_spend')
    attempt = models.OneToOneField(WalletSpendAttempt, on_delete=models.PROTECT, null=True, blank=True)
    intent = models.JSONField(default=dict)
    blocker = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
