"""Shared short-transaction reserve / dispatch / finalize protocol.

Lock order: domain row (if any), wallet, attempt. One active spend per wallet
intentionally serializes provenance. Unknown never expires and never re-dispatches.
"""
import hashlib
import json
from decimal import Decimal
from django.db import transaction, connection
from rest_framework.exceptions import ValidationError
from django.core.exceptions import ValidationError as ModelValidationError
from .models import ClientWallet, WalletSpendAttempt, WalletSpendAudit
from .services import get_or_lock_wallet, write_wallet_ledger
from .diamonds import coin_units_per_yuan, yuan_to_coin_units
from .coin_sync import coin_backed_wallet_buckets, has_pending_coin_refunds

ZERO = Decimal('0.00')


def fail(code):
    raise ValidationError({'code': code, 'detail': code})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def reserved(wallet, exclude=None):
    from django.db.models import Sum
    qs = WalletSpendAttempt.objects.filter(wallet=wallet, status__in=WalletSpendAttempt.ACTIVE)
    if exclude is not None:
        qs = qs.exclude(pk=exclude)
    return qs.aggregate(v=Sum('reserved_amount'))['v'] or ZERO


def reserve(profile, *, kind, business_no, key, amount, intent, user_ip='127.0.0.1'):
    from .coin_balance_service import _reserved_checkout_amount
    if kind not in ('gift', 'surcharge', 'order_checkout', 'order') or not key or len(key) > 100:
        fail('INVALID_REQUEST')
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal('.01')):
        fail('INVALID_AMOUNT')
    fingerprint = digest({'kind': kind, 'business_no': business_no, 'amount': str(amount.quantize(Decimal('.01'))), 'intent': intent})
    with transaction.atomic():
        wallet = get_or_lock_wallet(profile)
        previous = WalletSpendAttempt.objects.filter(wallet=wallet, kind=kind, idempotency_key=key).first()
        if previous:
            if previous.request_digest != fingerprint:
                fail('IDEMPOTENCY_CONFLICT')
            return previous
        if reserved(wallet):
            fail('PAYMENT_PENDING')
        from .spend_models import OrderWalletSpend
        if OrderWalletSpend.objects.filter(order__boss_user_id=profile.user_id).exclude(blocker='').exists():
            fail('LEGACY_COIN_REVIEW_REQUIRED')
        checkout_reserved = _reserved_checkout_amount(profile,
            exclude_order_no=business_no if kind in ('order', 'order_checkout') else '')
        if checkout_reserved:
            # Until checkout provenance is allocated per lot, do not consume
            # any coin source owned by an unfinished checkout, even if manual
            # value makes the aggregate available balance look sufficient.
            fail('CHECKOUT_RECOVERY_PENDING')
        if wallet.balance < amount:
            fail('INSUFFICIENT_BALANCE')
        if has_pending_coin_refunds(profile):
            fail('COIN_REFUND_PENDING')
        scale = coin_units_per_yuan()
        buckets = coin_backed_wallet_buckets(profile)
        if any(value > ZERO and bucket_scale != scale for bucket_scale, value in buckets.items()):
            fail('COIN_SCALE_CONFLICT')
        backed = min(amount, buckets.get(scale, ZERO))
        units = yuan_to_coin_units(backed, units_per_yuan=scale) if backed else 0
        allocations = []
        remaining = backed
        for lot in coin_backed_wallet_buckets(profile, with_lots=True):
            take = min(remaining, lot['amount'])
            if take:
                allocations.append({**lot, 'amount': str(take)})
                remaining -= take
            if not remaining:
                break
        attempt = WalletSpendAttempt(wallet=wallet, kind=kind, business_no=business_no,
            idempotency_key=key, request_digest=fingerprint, amount=amount, reserved_amount=amount,
            source_snapshot={'coin_amount': str(backed), 'local_amount': str(amount-backed),
                             'coin_units': units, 'scale': scale, 'allocations': allocations})
        from apps.payments.virtualpay import virtual_env
        attempt.request_payload = {'openid': profile.openid, 'env': virtual_env(),
            'user_ip': user_ip or '127.0.0.1', 'amount': units, 'order_id': attempt.external_id,
            'payitem': json.dumps([{'productid': f'{kind}_{business_no}'[:64], 'unit_price': units, 'quantity': 1}], separators=(',', ':')),
            'remark': f'{kind}:{business_no}'[:64]}
        attempt.save()
        WalletSpendAudit.objects.create(attempt=attempt, event='reserved', detail={'digest': fingerprint})
        return attempt


def _locked(attempt_id):
    # Every phase uses the same parent-order -> wallet -> attempt order.
    from apps.orders.models import Order
    from apps.orders.surcharge_models import OrderSurcharge
    order_id = OrderSurcharge.objects.filter(attempt_id=attempt_id).values_list('order_id', flat=True).first()
    if order_id is None:
        from .spend_models import OrderWalletSpend
        order_id = OrderWalletSpend.objects.filter(attempt_id=attempt_id).values_list('order_id', flat=True).first()
    if order_id is not None:
        Order.objects.select_for_update().get(pk=order_id)
    wallet_id = WalletSpendAttempt.objects.values_list('wallet_id', flat=True).get(pk=attempt_id)
    wallet = ClientWallet.objects.select_for_update().get(pk=wallet_id)
    return wallet, WalletSpendAttempt.objects.select_for_update().get(pk=attempt_id)


def cancel_prepared(attempt_id, *, user=None, reason='PREPARED_CANCELLED', strict=True):
    """Only a locked, never-dispatched intent may relinquish its reservation.

    No age/TTL condition exists. Unknown and dispatching are never released.
    user=None is for trusted internal recovery, not a public permission bypass.
    """
    with transaction.atomic():
        wallet, attempt = _locked(attempt_id)
        if user is not None and wallet.profile.user_id != user.pk:
            from django.http import Http404
            raise Http404
        if attempt.status == 'failed':
            return attempt
        if (attempt.status != 'prepared' or attempt.evidence or
            attempt.audit_events.filter(event__in=('dispatching', 'unknown', 'succeeded', 'recovery_query', 'completed')).exists()):
            if strict:
                fail('PAYMENT_PENDING')
            return attempt
        attempt.status = 'failed'
        attempt.reserved_amount = ZERO
        attempt.blocker = reason
        attempt.save(update_fields=['status', 'reserved_amount', 'blocker', 'updated_at'])
        if attempt.kind == 'gift':
            from apps.gifts.models import GiftPurchase
            GiftPurchase.objects.filter(attempt=attempt).update(status='failed')
        else:
            from apps.orders.surcharge_models import OrderSurcharge
            OrderSurcharge.objects.filter(attempt=attempt).update(status='failed')
        WalletSpendAudit.objects.create(attempt=attempt, event='prepared_cancelled', detail={'reason': reason})
        return attempt


def finalize(attempt_id, apply=None):
    if apply is None and WalletSpendAttempt.objects.filter(pk=attempt_id, kind='order').exists():
        from .order_spend import fulfill
        apply = fulfill
    with transaction.atomic():
        wallet, attempt = _locked(attempt_id)
        if attempt.status == 'completed':
            return attempt
        if attempt.status != 'succeeded':
            return attempt
        # Reservation is removed in this same transaction, never before completion.
        attempt.reserved_amount = ZERO
        attempt.status = 'completed'
        attempt.save(update_fields=['reserved_amount', 'status', 'updated_at'])
        _, created = write_wallet_ledger(wallet, 'shared_spend', -attempt.amount,
            reference_type=attempt.kind, reference_id=attempt.external_id,
            note=f'{attempt.kind}:{attempt.business_no}',
            checkout_order_no=attempt.business_no if attempt.kind in ('order', 'order_checkout') else '')
        if created:
            wallet.spent_total += attempt.amount
            wallet.save(update_fields=['spent_total', 'updated_at'])
        if apply:
            apply(attempt)
        WalletSpendAudit.objects.create(attempt=attempt, event='completed', detail={})
        return attempt


def execute(attempt_id, *, adapter=None, code='', apply=None):
    if connection.in_atomic_block:
        raise RuntimeError('Shared external dispatch must run outside enclosing transactions')
    attempt = WalletSpendAttempt.objects.get(pk=attempt_id)
    if attempt.status == 'completed':
        return attempt
    if attempt.status == 'succeeded':
        return finalize(attempt_id, apply)
    if attempt.status != 'prepared':
        return attempt
    if attempt.kind == 'order_checkout':
        # A closed checkout gate must not even exchange a login code. Keep the
        # locked authorization below as well: flags may change during auth.
        from apps.orders.checkout_payment import ensure_dispatch_enabled
        try:
            ensure_dispatch_enabled(attempt)
        except ValidationError:
            cancel_prepared(attempt_id, reason='AUTHORIZATION_REVOKED', strict=False)
            raise
    if attempt.source_snapshot['coin_units']:
        if adapter is None:
            from .spend_adapter import WeChatSpendAdapter
            adapter = WeChatSpendAdapter()
        try:
            session = adapter.authenticate(attempt, code)
        except Exception:
            # authenticate is contractually non-financial; no spend was issued.
            # A concurrent dispatch may already have won, so never force-release.
            cancel_prepared(attempt_id, reason='AUTHENTICATION_FAILED', strict=False)
            raise
    else:
        session = None
    authorization_error = None
    with transaction.atomic():
        _, attempt = _locked(attempt_id)
        if attempt.status != 'prepared':
            return attempt
        try:
            if attempt.kind == 'gift':
                from apps.gifts.services.purchases import authorize_dispatch
            elif attempt.kind == 'order_checkout':
                from apps.orders.checkout_payment import authorize_dispatch
            elif attempt.kind == 'order':
                from .order_spend import authorize_dispatch
            else:
                from apps.orders.surcharge_payment import authorize_dispatch
            authorization = authorize_dispatch(attempt)
        except (ValidationError, ModelValidationError) as exc:
            authorization_error = exc
            cancel_prepared(attempt_id, reason='AUTHORIZATION_REVOKED')
        if authorization_error is None:
            attempt.status = 'dispatching'
            attempt.save(update_fields=['status', 'updated_at'])
            WalletSpendAudit.objects.create(attempt=attempt, event='dispatching', detail={
                'authorization': authorization, 'authorization_digest': digest(authorization)})
    if authorization_error is not None:
        raise authorization_error
    try:
        evidence = adapter.spend(attempt, session) if attempt.source_snapshot['coin_units'] else {'local_only': True}
        succeeded = not attempt.source_snapshot['coin_units'] or (
            evidence.get('errcode') in (0, 268490004) and evidence.get('order_id') == attempt.external_id)
    except Exception:
        evidence = {}
        succeeded = False
    with transaction.atomic():
        _, attempt = _locked(attempt_id)
        if attempt.status in ('completed', 'failed'):
            return attempt
        if attempt.status == 'succeeded':
            # A concurrent exact-evidence recovery won; never downgrade its
            # proof or reopen a terminal capture with this late response.
            succeeded = True
        else:
            attempt.status = 'succeeded' if succeeded else 'unknown'
            attempt.evidence = evidence
            attempt.blocker = '' if succeeded else 'REMOTE_RESULT_UNKNOWN'
            attempt.save(update_fields=['status', 'evidence', 'blocker', 'updated_at'])
            WalletSpendAudit.objects.create(attempt=attempt, event=attempt.status, detail={})
    return finalize(attempt_id, apply) if succeeded else attempt


def recover(attempt_id, *, adapter=None, apply=None):
    """Cancel provably undispatched prepared; otherwise query/finalize, never re-pay."""
    attempt = WalletSpendAttempt.objects.get(pk=attempt_id)
    if attempt.status == 'prepared':
        return cancel_prepared(attempt_id, reason='PREPARED_RECOVERY_CANCELLED', strict=False)
    if attempt.status in ('completed', 'failed'):
        return attempt
    if attempt.status == 'succeeded':
        return finalize(attempt_id, apply)
    if adapter is None:
        from .spend_adapter import WeChatSpendAdapter
        adapter = WeChatSpendAdapter()
    try:
        evidence = adapter.query(attempt)
    except Exception:
        evidence = None
    with transaction.atomic():
        _, attempt = _locked(attempt_id)
        if attempt.status not in ('dispatching', 'unknown'):
            return attempt
        # A query adapter must prove the exact immutable request and amount.
        proven = bool(evidence and evidence.get('order_id') == attempt.external_id
            and evidence.get('amount') == attempt.source_snapshot['coin_units']
            and evidence.get('openid') == attempt.request_payload['openid']
            and evidence.get('env') == attempt.request_payload['env']
            and evidence.get('status') == 'succeeded')
        attempt.status = 'succeeded' if proven else 'unknown'
        attempt.blocker = '' if proven else 'OFFICIAL_QUERY_UNAVAILABLE'
        if proven:
            attempt.evidence = evidence
        attempt.save(update_fields=['status', 'blocker', 'evidence', 'updated_at'])
        WalletSpendAudit.objects.create(attempt=attempt, event='recovery_query', detail={'proven': proven})
    return finalize(attempt_id, apply) if proven else attempt

