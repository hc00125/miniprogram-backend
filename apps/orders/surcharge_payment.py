"""Independent surcharge payment; base Order.paid is never assigned here."""
import uuid
from decimal import Decimal
from django.conf import settings
from django.db import transaction, connection
from django.utils import timezone
from django.shortcuts import get_object_or_404
from apps.wallet import spend_service as spend
from apps.wallet.services import get_or_lock_wallet
from apps.gifts.services.purchases import profile_for, lock_authorization
from .models import Order
from .surcharge_models import OrderSurcharge
from .cancellation_models import OrderReplacementState


def policy():
    # Unallocated/ non-refundable surcharge money is never a production product.
    # Only the offline, network-denied test settings opt in for safety exercises.
    if not getattr(settings, 'ORDER_SURCHARGE_ISOLATED_TEST_MODE', False):
        spend.fail('SURCHARGE_LIFECYCLE_UNAVAILABLE')
    p = getattr(settings, 'ORDER_SURCHARGE_POLICY', {})
    if not p.get('version') or p.get('platform_approved') is not True or p.get('rules_approved') is not True:
        spend.fail('POLICY_UNCONFIRMED')
    if any(type(p.get(k)) is not int or p[k] <= 0 for k in ('min_diamonds', 'max_diamonds', 'daily_diamonds')):
        spend.fail('POLICY_UNCONFIRMED')
    return p


def prepare(order_no, user, *, amount_diamonds, idempotency_key):
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 100 or type(amount_diamonds) is not int:
        spend.fail('INVALID_REQUEST')
    fingerprint = spend.digest({'order_no': order_no, 'amount_diamonds': amount_diamonds})
    profile = profile_for(user)
    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), order_no=order_no, boss_user=user)
        get_or_lock_wallet(profile)
        previous = OrderSurcharge.objects.filter(boss=user, idempotency_key=idempotency_key).first()
        if previous:
            if previous.request_digest != fingerprint:
                spend.fail('IDEMPOTENCY_CONFLICT')
            return previous
        # v2 admits surcharge only in the transaction that creates its parent.
        # Existing keys remain recoverable; no first-time standalone append.
        spend.fail('SURCHARGE_PREORDER_ONLY')


def authorize_dispatch(attempt):
    item = OrderSurcharge.objects.filter(attempt=attempt).first()
    if item is None:
        return
    if not getattr(settings, 'ORDER_SURCHARGE_ENABLED', False):
        spend.fail('FEATURE_DISABLED')
    p = policy()
    user, _ = lock_authorization(item.boss, 'order_surcharge')
    profile_for(user)
    if p['version'] != item.policy_snapshot['version'] or spend.digest(p) != item.policy_snapshot.get('policy_digest'):
        spend.fail('POLICY_UNCONFIRMED')
    from apps.gifts.models import Gift
    if not Gift.objects.select_for_update().filter(code='order_surcharge', kind='internal_surcharge', internal_enabled=True).first():
        spend.fail('SURCHARGE_CONFIG_UNAVAILABLE')
    order = Order.objects.get(pk=item.order_id)  # Parent already locked by spend._locked.
    if (order.order_type != 'normal' or order.fulfillment_mode != 'public' or
        order.target_player_id or order.designated_players or order.status != Order.STATUS_WAITING or
        order.order_players.exists() or OrderReplacementState.objects.filter(order=order).exclude(status='resolved').exists()):
        spend.fail('ORDER_NOT_ELIGIBLE')
    if not p['min_diamonds'] <= item.amount_diamonds <= p['max_diamonds']:
        spend.fail('INVALID_AMOUNT')
    return {'schema_version': 1, 'policy_digest': spend.digest(p),
        'order_id': order.pk, 'order_status': order.status, 'fulfillment_mode': order.fulfillment_mode,
        'buyer_id': user.pk, 'buyer_active': user.is_active, 'account_status': profile_for(user).account_status,
        'amount_diamonds': item.amount_diamonds, 'internal_gift_code': 'order_surcharge'}


def fulfill(attempt):
    item = OrderSurcharge.objects.select_for_update().get(attempt=attempt)
    item.status = 'paid'
    item.save(update_fields=['status', 'updated_at'])


def pay(order_no, user, *, amount_diamonds, idempotency_key, code='', adapter=None):
    item = prepare(order_no, user, amount_diamonds=amount_diamonds, idempotency_key=idempotency_key)
    result = spend.execute(item.attempt_id, code=code, adapter=adapter, apply=fulfill)
    if result.status == 'unknown':
        OrderSurcharge.objects.filter(pk=item.pk).update(status='unknown')
    item.refresh_from_db()
    return item
