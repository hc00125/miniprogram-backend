"""Internal shared-finalize hook. Never performs a payment or opens purchases."""
from datetime import datetime, timedelta
from django.db import transaction
from django.utils import timezone
from apps.wallet.spend_service import fail
from .evidence import lock_confirmed_payment
from .income import credit_purchase
from .models import CrownGrant, PatronagePurchase
from .pricing import PACKAGES


@transaction.atomic
def fulfill(attempt, *, confirmed_at=None):
    """Future `spend.finalize(id, apply=fulfill)` callback; returns the purchase.

    Caller must pass a trusted, aware confirmation timestamp or omit it for the
    local completion time. Never pass client input. A replay keeps the original.
    Locks: client wallet -> attempt -> purchase -> player income wallet.
    The buyer's unique ClientWallet serializes all their purchases (a stronger
    lock than boss/player), including the empty-first-grant case. Other bosses
    remain independent. No Player row/order-acceptance lock is acquired.
    """
    purchase_id = PatronagePurchase.objects.filter(attempt_id=attempt.pk).values_list('pk', flat=True).first()
    if purchase_id is None:
        fail('PATRONAGE_PAYMENT_UNCONFIRMED')
    purchase, locked_attempt, debit = lock_confirmed_payment(purchase_id, require_paid=False)
    if purchase.paid_at is None:
        confirmed_at = confirmed_at or timezone.now()
        if not isinstance(confirmed_at, datetime) or timezone.is_naive(confirmed_at):
            fail('INVALID_PAYMENT_TIMESTAMP')
        purchase.paid_at = confirmed_at
    purchase.payment_status = 'paid'
    purchase.save(update_fields=['payment_status', 'paid_at'])
    source = 'day_pass_bonus' if purchase.package_code == 'day_pass' else 'purchase'
    days = purchase.bonus_naming_days if source == 'day_pass_bonus' else dict((p[0], p[2]) for p in PACKAGES)[purchase.package_code]
    grant = CrownGrant.objects.filter(purchase=purchase, source=source).first()
    if grant is None:
        # All writers run under the same buyer-wallet lock, including callers
        # inside shared finalize. Existing grant snapshots are never extended
        # in place: one append-only source records the purchased increment.
        previous = CrownGrant.objects.filter(purchase__boss_id=purchase.boss_id,
            purchase__player_id=purchase.player_id, purchase__payment_status='paid',
            revoked_at__isnull=True, expires_at__gt=purchase.paid_at).order_by('-expires_at', '-pk').first()
        extension_start = max(purchase.paid_at, previous.expires_at) if previous else purchase.paid_at
        grant = CrownGrant.objects.create(purchase=purchase, source=source,
            package_name='周冠' if source == 'day_pass_bonus' else purchase.package_name,
            starts_at=purchase.paid_at, extension_starts_at=extension_start,
            duration_days=days, expires_at=extension_start+timedelta(days=days))
    purchase.starts_at, purchase.expires_at = grant.starts_at, grant.expires_at
    purchase.save(update_fields=['starts_at', 'expires_at'])
    credit_purchase(purchase)
    return purchase
