"""Original order capture. Each phase locks order -> wallet -> attempt.

No external call in prepare/finalize/cancel. Recovery is not a new purchase:
only first dispatch checks current authorization. Historical ambiguity stays
an explicit manual blocker, never a newly fabricated failed attempt.
"""
from decimal import Decimal
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from apps.orders.models import Order
from apps.orders.surcharge_models import OrderCheckout
from apps.payments.models import Payment
from apps.payments.services import ensure_order_owner, get_order_amount, generate_payment_no, mark_payment_paid
from apps.orders.payment_deadlines import ensure_payment_window_open, payment_deadline_at, _local_coin_checkout_is_credited
from .spend_models import OrderWalletSpend, WalletSpendAttempt
from .services import get_or_lock_wallet
from . import spend_service as spend


def intent_for(order, amount, allow_recovery=False):
    return {'order_id': order.pk, 'order_no': order.order_no, 'boss_id': order.boss_user_id,
        'amount': str(amount), 'package_id': order.package_id, 'spec_id': order.spec_id,
        'required_players': order.required_players, 'fulfillment_mode': order.fulfillment_mode,
        'order_type': order.order_type, 'designated_players': order.designated_players,
        'target_player_id': order.target_player_id, 'parent_order_id': order.parent_order_id,
        'booked_hours': str(order.booked_hours),
        'allow_credited_checkout_recovery': bool(allow_recovery)}


def _profile(user, lock=False):
    from django.contrib.auth import get_user_model
    from apps.accounts.models import ClientProfile
    users, profiles = get_user_model().objects, ClientProfile.objects
    if lock:
        users, profiles = users.select_for_update(), profiles.select_for_update()
    current = users.get(pk=user.pk)
    profile = profiles.filter(user=current).first()
    if not current.is_active or not profile or profile.account_status != ClientProfile.ACCOUNT_STATUS_ACTIVE:
        spend.fail('ACCOUNT_RESTRICTED')
    return profile


def prepare(order_no, user, *, user_ip='127.0.0.1', allow_credited_checkout_recovery=False):
    # Resume before payment-window or account checks: remote capture is already
    # authorized; expired code/account/window cannot turn recovery into a charge.
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        spend.fail('ORDER_NOT_FOUND')
    ensure_order_owner(order, user)
    previous = OrderWalletSpend.objects.select_related('attempt').filter(order=order).first()
    if previous:
        if previous.blocker:
            spend.fail(previous.blocker)
        return previous.attempt
    ensure_payment_window_open(order_no, user,
        allow_credited_checkout_recovery=allow_credited_checkout_recovery)
    # Legacy in-flight goods payments are verified outside all local locks.
    from apps.payments.virtualpay import VIRTUAL_CHANNEL, VIRTUAL_MODE_GOODS, query_virtual_payment
    verified = set()
    for payment in Payment.objects.filter(order=order, status='paying'):
        if (payment.channel != VIRTUAL_CHANNEL or payment.scene != VIRTUAL_MODE_GOODS or
            not payment.expires_at or payment.expires_at > timezone.now()):
            spend.fail('PAYMENT_PENDING')
        synced = query_virtual_payment(payment.payment_no, user)
        if synced.status == 'paid' or synced.order.paid:
            spend.fail('ORDER_ALREADY_PAID')
        verified.add(payment.pk)
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        ensure_order_owner(order, user)
        previous = OrderWalletSpend.objects.select_related('attempt').filter(order=order).first()
        if previous:
            if previous.blocker:
                spend.fail(previous.blocker)
            return previous.attempt
        if order.paid or order.status != Order.STATUS_PENDING_PAYMENT:
            spend.fail('ORDER_NOT_ELIGIBLE')
        amount = get_order_amount(order)
        if not amount.is_finite() or amount <= 0:
            spend.fail('INVALID_AMOUNT')
        for payment in Payment.objects.select_for_update().filter(order=order, status__in=('paying', 'paid')):
            if (payment.pk not in verified or payment.status == 'paid' or
                not payment.expires_at or payment.expires_at > timezone.now()):
                spend.fail('PAYMENT_PENDING')
            payment.status = 'closed'
            payment.save(update_fields=['status', 'updated_at'])
        profile = _profile(user)
        get_or_lock_wallet(profile)
        intent = intent_for(order, amount, allow_credited_checkout_recovery)
        attempt = spend.reserve(profile, kind='order', business_no=order.order_no,
            key='order:' + str(order.pk), amount=amount, intent=intent, user_ip=user_ip)
        OrderWalletSpend.objects.create(order=order, attempt=attempt, intent=intent)
        OrderCheckout.objects.filter(order=order, surcharge__isnull=True).update(attempt=attempt, status='processing')
        return attempt


def authorize_dispatch(attempt):
    binding = OrderWalletSpend.objects.select_related('order').get(attempt=attempt)
    order = binding.order
    profile = _profile(order.boss_user, lock=True)
    if profile.pk != attempt.wallet.profile_id or profile.openid != attempt.request_payload['openid']:
        spend.fail('IDENTITY_CHANGED')
    if binding.blocker or order.paid or order.status != Order.STATUS_PENDING_PAYMENT:
        spend.fail('ORDER_NOT_ELIGIBLE')
    amount = get_order_amount(order, wallet_attempt=attempt)
    current = intent_for(order, amount, binding.intent['allow_credited_checkout_recovery'])
    if current != binding.intent or attempt.request_digest != spend.digest({
        'kind': 'order', 'business_no': order.order_no, 'amount': str(amount), 'intent': current}):
        spend.fail('PRICE_CHANGED')
    if attempt.source_snapshot['coin_units'] and (not getattr(settings, 'SHARED_SPEND_PLATFORM_APPROVED', False)
            or not getattr(settings, 'WECHAT_VIRTUALPAY_ENABLED', False)):
        spend.fail('POLICY_UNCONFIRMED')
    deadline = payment_deadline_at(order)
    if deadline and deadline <= timezone.now() and not (
        current['allow_credited_checkout_recovery'] and _local_coin_checkout_is_credited(order)):
        spend.fail('PAYMENT_WINDOW_CLOSED')
    if Payment.objects.filter(order=order, status__in=('paying', 'paid')).exists():
        spend.fail('PAYMENT_PENDING')
    return current


def fulfill(attempt):
    """Only exact succeeded capture may settle; do NOT re-run purchase policy."""
    binding = OrderWalletSpend.objects.select_related('order').get(attempt=attempt)
    order = binding.order
    if binding.blocker or order.boss_user_id != attempt.wallet.profile.user_id:
        spend.fail('CAPTURE_REQUIRES_REVIEW')
    if order.paid or order.status == Order.STATUS_CANCELLED:
        spend.fail('CAPTURE_REQUIRES_REVIEW')
    source = attempt.source_snapshot
    if sum((Decimal(lot['amount']) for lot in source['allocations']), Decimal('0')) != Decimal(source['coin_amount']):
        spend.fail('SOURCE_AUDIT_REQUIRED')
    evidence = attempt.evidence
    if source['coin_units'] and not (evidence.get('order_id') == attempt.external_id and (
            evidence.get('errcode') in (0, 268490004) or (
            evidence.get('status') == 'succeeded' and evidence.get('amount') == source['coin_units'] and
            evidence.get('openid') == attempt.request_payload['openid'] and
            evidence.get('env') == attempt.request_payload['env']))):
        spend.fail('CAPTURE_EVIDENCE_REQUIRED')
    payload = {'balance_payment': True, 'wallet_attempt': attempt.external_id,
        'source_snapshot': source, 'wechat_coin_units': source['coin_units'],
        'wechat_coin_units_per_yuan': source['scale'],
        'wechat_coin_amount_yuan': source['coin_amount'],
        'wechat_coin_diamonds': format(Decimal(source['coin_amount']) * 10, '.1f'),
        'wechat_coin_status': 'succeeded' if source['coin_units'] else 'not_required',
        'wechat_coin_order_id': attempt.external_id if source['coin_units'] else '',
        'wechat_coin_response': attempt.evidence}
    payment = Payment.objects.create(payment_no=generate_payment_no(), order=order, channel='balance',
        scene='balance', amount=attempt.amount, status='paying', notify_payload=payload)
    mark_payment_paid(payment, attempt.external_id, payload)
    OrderCheckout.objects.filter(order=order, surcharge__isnull=True).update(status='paid')


def pay(order_no, user, *, code='', user_ip='127.0.0.1', allow_credited_checkout_recovery=False,
        idempotency_key=None):
    if connection.in_atomic_block:
        raise RuntimeError('Original wallet payment requires committed prepare before dispatch')
    # Deferred refund sync is only for committed local refunds, outside the
    # capture transaction. Cancellation and capture recovery never call it.
    if not OrderWalletSpend.objects.filter(order__order_no=order_no).exists():
        order = Order.objects.filter(order_no=order_no).first()
        if order is None:
            spend.fail('ORDER_NOT_FOUND')
        ensure_order_owner(order, user)
        profile = _profile(user)
        from .coin_sync import has_pending_coin_refunds, _request_session, sync_pending_coin_refunds
        if has_pending_coin_refunds(profile):
            if not getattr(settings, 'SHARED_SPEND_PLATFORM_APPROVED', False):
                spend.fail('POLICY_UNCONFIRMED')
            sync_pending_coin_refunds(profile, _request_session(profile, code), user_ip)
    attempt = prepare(order_no, user, user_ip=user_ip,
        allow_credited_checkout_recovery=allow_credited_checkout_recovery)
    result = spend.execute(attempt.pk, code=code, apply=fulfill)
    return payment_data(order_no, result)


def payment_data(order_no, result):
    wallet = result.wallet
    wallet.refresh_from_db()
    payment = Payment.objects.filter(order__order_no=order_no, notify_payload__wallet_attempt=result.external_id).first()
    from apps.orders.checkout import checkout_data
    data = {'order_no': order_no, 'payment_no': payment.payment_no if payment else None,
        'status': {'completed': 'paid', 'dispatching': 'unknown', 'prepared': 'processing',
                   'succeeded': 'processing'}.get(result.status, result.status),
        'amount': format(result.amount, '.2f'), 'balance': format(wallet.balance, '.2f'),
        'wechat_coin_units': result.source_snapshot['coin_units'],
        'wechat_coin_units_per_yuan': result.source_snapshot['scale'],
        'wechat_coin_diamonds': format(Decimal(result.source_snapshot['coin_amount']) * 10, '.1f')}
    checkout = checkout_data(Order.objects.get(order_no=order_no))
    if checkout is not None:
        data['checkout'] = checkout
    return data


def guard_order(order, *, cancel=False, wallet_attempt=None):
    """Must be called under parent lock before any payment/refund side effect."""
    if order.source == Order.SOURCE_STAFF:
        raise ValidationError({'code': 'STAFF_OFFLINE_ORDER', 'detail': '客服代派订单已在线下收款；付款、退款请联系原客服，不走钻石钱包'})
    binding = OrderWalletSpend.objects.select_related('attempt').filter(order=order).first()
    if not binding:
        return
    if binding.blocker:
        spend.fail(binding.blocker)
    a = binding.attempt
    if a and a.status in WalletSpendAttempt.ACTIVE and (not wallet_attempt or wallet_attempt.pk != a.pk):
        if cancel and a.status == 'prepared':
            spend.cancel_prepared(a.pk, reason='ORDER_CANCELLED')
        else:
            spend.fail('ORDER_PAYMENT_PENDING')


def recover_order(order):
    """Existing query/timeout paths may finish capture, never dispatch a debit."""
    binding = OrderWalletSpend.objects.select_related('attempt').filter(order=order).first()
    if binding and not binding.blocker and binding.attempt_id:
        if binding.attempt.status in ('succeeded', 'dispatching', 'unknown'):
            return spend.recover(binding.attempt_id, apply=fulfill)
    return None


def query_existing(order_no, user, *, include_prepared=True):
    """Owner-scoped recovery DTO for the old query/finalize routes. Never pay."""
    binding = OrderWalletSpend.objects.select_related('order', 'attempt').filter(
        order__order_no=order_no, order__boss_user=user).first()
    if not binding:
        return None
    if binding.blocker:
        spend.fail(binding.blocker)
    if not include_prepared and binding.attempt.status == 'prepared':
        return None
    result = recover_order(binding.order) or binding.attempt
    return payment_data(order_no, result)
