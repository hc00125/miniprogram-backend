"""Combined checkout via the existing balance endpoint and durable spend engine.

Production admission is explicit and defaults closed. Base Payment never
includes surcharge, which is separately conserved by the checkout snapshot.
"""
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from apps.wallet import spend_service as spend
from apps.wallet.services import get_or_lock_wallet
from apps.gifts.services.purchases import profile_for
from .models import Order
from .surcharge_models import OrderCheckout, OrderSurcharge
from .surcharges import quote_amount
from .checkout import checkout_data


def ensure_enabled():
    from .checkout_gates import checkout_blockers
    blockers = checkout_blockers()
    if blockers:
        spend.fail(blockers[0])


def validate_checkout(item, order):
    if (order.paid or order.status != Order.STATUS_PENDING_PAYMENT or order.order_players.exists()
        or order.fulfillment_mode != 'public' or order.order_type != 'normal' or order.designated_players):
        spend.fail('ORDER_NOT_ELIGIBLE')
    if Decimal(str(order.total_amount)) != item.base_amount:
        spend.fail('PRICE_CHANGED')
    if not item.surcharge_id or item.surcharge.allocation_snapshot != quote_amount(
            item.surcharge.amount_diamonds, order.required_players):
        spend.fail('SURCHARGE_SNAPSHOT_UNAVAILABLE')
    if item.surcharge.refunded_diamonds or item.total_amount != item.base_amount + item.surcharge.amount_yuan:
        spend.fail('INVALID_AMOUNT')


def ensure_dispatch_enabled(attempt):
    ensure_enabled()
    if attempt.source_snapshot['coin_units'] and not getattr(settings, 'WECHAT_VIRTUALPAY_ENABLED', False):
        spend.fail('POLICY_UNCONFIRMED')


def _confirmed_closed_cashier(recharge):
    from apps.payments.services import amount_to_cents
    data = dict(recharge.notify_payload or {}).get('query_order') or {}
    return (recharge.status == 'closed' and not recharge.paid_at and not recharge.credited_at
        and data.get('status') == 6 and not data.get('paid_fee') and not data.get('paid_time')
        and data.get('order_fee') == amount_to_cents(recharge.amount)
        and (not data.get('order_id') or data['order_id'] == recharge.recharge_no))


def _query_checkout_recharge(recharge, user):
    """Only an authoritative XPay CLOSED result releases a cancelled cashier."""
    from apps.wallet.coin_recharge import query_coin_recharge
    from apps.wallet.models import RechargeOrder
    from apps.payments.services import amount_to_cents
    synced = query_coin_recharge(recharge.recharge_no, user)
    with transaction.atomic():
        synced = RechargeOrder.objects.select_for_update().get(pk=synced.pk)
        data = dict(synced.notify_payload or {}).get('query_order') or {}
        if (synced.status in ('created', 'paying') and not synced.paid_at and not synced.credited_at
            and data.get('status') == 6 and not data.get('paid_fee') and not data.get('paid_time')
            and data.get('order_fee') == amount_to_cents(synced.amount)
            and (not data.get('order_id') or data['order_id'] == synced.recharge_no)):
            synced.status = RechargeOrder.STATUS_CLOSED
            synced.save(update_fields=['status', 'updated_at'])
    return synced


def query_coin_payment(order_no, user):
    from django.utils import timezone
    from apps.wallet.coin_recharge import latest_checkout_coin_recharge, coin_recharge_payload
    from .payment_deadlines import payment_deadline_at, expire_due_unpaid_order
    order = get_object_or_404(Order, order_no=order_no, boss_user=user)
    recharge = latest_checkout_coin_recharge(profile_for(user), order_no)
    if not recharge:
        return {'found': False, 'order_no': order_no}
    recharge = _query_checkout_recharge(recharge, user)
    item = OrderCheckout.objects.get(order=order, boss=user)
    if _confirmed_closed_cashier(recharge) and not item.attempt_id:
        expire_due_unpaid_order(order)
        if order.status == Order.STATUS_CANCELLED and not order.paid and item.status == 'created':
            # Older timeout code could cancel the business order but leave its
            # unpaid checkout open. Repair only after verified remote closure.
            from .surcharge_refunds import cancel_unpaid_checkout
            cancel_unpaid_checkout(order)
    order.refresh_from_db()
    deadline = payment_deadline_at(order)
    retry = (not order.paid and order.status == Order.STATUS_PENDING_PAYMENT and not item.attempt_id
        and recharge.amount == item.total_amount and _confirmed_closed_cashier(recharge)
        and bool(deadline and deadline > timezone.now()))
    return {**coin_recharge_payload(recharge), 'found': True, 'retry_allowed': retry,
        'retry_from_payment_no': dict(recharge.notify_payload or {}).get('retry_from_payment_no', ''),
        'order_status': order.status}


def create_coin_payment(order_no, user, *, code, platform, retry_from_payment_no=''):
    """Reuse ordinary order coin recharge, but freeze the combined amount.

    The parent lock serializes balance payment and concurrent recharge creation.
    Creating the cashier parameters is not a debit or a paid business order.
    """
    from apps.wallet.coin_recharge import create_coin_recharge, latest_checkout_coin_recharge
    from apps.wallet.models import RechargeOrder
    if retry_from_payment_no:
        original = get_object_or_404(RechargeOrder, recharge_no=retry_from_payment_no,
            profile__user=user, checkout_order_no=order_no)
        original = _query_checkout_recharge(original, user)
        if not _confirmed_closed_cashier(original):
            spend.fail('CHECKOUT_RECHARGE_PENDING')
    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), order_no=order_no, boss_user=user)
        item = OrderCheckout.objects.select_for_update().get(order=order, boss=user)
        ensure_enabled()
        validate_checkout(item, order)
        if item.attempt_id:
            spend.fail('PAYMENT_PENDING')
        latest = latest_checkout_coin_recharge(profile_for(user), order_no)
        if retry_from_payment_no and (not latest or latest.recharge_no != retry_from_payment_no
            and dict(latest.notify_payload or {}).get('retry_from_payment_no') != retry_from_payment_no):
            spend.fail('CHECKOUT_RECHARGE_CHANGED')
        recharge, payload = create_coin_recharge(user, item.total_amount, code, platform, checkout_order_no=order_no)
        if retry_from_payment_no:
            if recharge.recharge_no == retry_from_payment_no:
                spend.fail('CHECKOUT_RECHARGE_PENDING')
            stored = dict(recharge.notify_payload or {})
            stored['retry_from_payment_no'] = retry_from_payment_no
            recharge.notify_payload = stored
            recharge.save(update_fields=['notify_payload', 'updated_at'])
            payload['retry_from_payment_no'] = retry_from_payment_no
        return recharge, payload


def prepare(order_no, user):
    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), order_no=order_no, boss_user=user)
        item = OrderCheckout.objects.select_for_update().get(order=order, boss=user)
        if item.attempt_id:
            # Ownership remains mandatory; finishing an authorized capture is
            # not a new purchase. Prepared still reauthorizes before dispatch.
            return item
        profile = profile_for(user)
        ensure_enabled()
        validate_checkout(item, order)
        from apps.wallet.coin_recharge import latest_checkout_coin_recharge
        from apps.wallet.models import RechargeOrder
        recharge = latest_checkout_coin_recharge(profile, order_no)
        if recharge and recharge.status in (RechargeOrder.STATUS_CREATED, RechargeOrder.STATUS_PAYING, RechargeOrder.STATUS_PAID):
            spend.fail('CHECKOUT_RECHARGE_PENDING')
        if recharge and recharge.status == RechargeOrder.STATUS_CREDITED and recharge.amount != item.total_amount:
            spend.fail('CHECKOUT_RECHARGE_AMOUNT_MISMATCH')
        from apps.payments.models import Payment
        if Payment.objects.filter(order=order, status__in=('paying', 'paid')).exists():
            spend.fail('PAYMENT_PENDING')
        get_or_lock_wallet(profile)
        attempt = spend.reserve(profile, kind='order_checkout', business_no=order.order_no,
            key=item.idempotency_key, amount=item.total_amount,
            intent={'order_no': order_no, 'quote_version': item.quote_snapshot['quote_version'],
                    'base_yuan': str(item.base_amount), 'surcharge_yuan': str(item.surcharge_amount)})
        item.attempt = attempt
        item.status = 'processing'
        item.save(update_fields=['attempt', 'status'])
        OrderSurcharge.objects.filter(pk=item.surcharge_id).update(attempt=attempt,
            attempt_reference=attempt.external_id, status='processing')
        return item


def authorize_dispatch(attempt):
    item = OrderCheckout.objects.select_related('surcharge', 'order', 'boss').get(attempt=attempt)
    ensure_dispatch_enabled(attempt)
    # Same order -> wallet -> attempt -> user -> profile order as original
    # durable payments; eligibility cannot change before dispatching commits.
    from apps.wallet.order_spend import _profile
    profile = _profile(item.boss, lock=True)
    if profile.pk != attempt.wallet.profile_id or profile.openid != attempt.request_payload['openid']:
        spend.fail('IDENTITY_CHANGED')
    validate_checkout(item, item.order)
    return {'schema_version': 2, 'quote_version': item.quote_snapshot['quote_version'],
            'order_id': item.order_id, 'amount_yuan': str(attempt.amount)}


def fulfill(attempt):
    from apps.payments.models import Payment
    from apps.payments.services import mark_payment_paid, generate_payment_no
    item = OrderCheckout.objects.select_for_update().select_related('order').get(attempt=attempt)
    if item.status == 'paid':
        return
    # Status transitions are inside the same ledger transaction. No base-only capture.
    OrderSurcharge.objects.filter(pk=item.surcharge_id).update(status='paid')
    item.status = 'paid'
    item.save(update_fields=['status'])
    payment = Payment.objects.create(order=item.order, payment_no=generate_payment_no(),
        channel='balance', scene='balance', amount=item.base_amount, status='paying',
        notify_payload={'checkout_attempt': attempt.external_id})
    mark_payment_paid(payment, attempt.external_id, {'checkout_attempt': attempt.external_id,
        'base_amount_yuan': str(item.base_amount), 'surcharge_amount_yuan': str(item.surcharge_amount),
        'combined_amount_yuan': str(item.total_amount), 'source_snapshot': attempt.source_snapshot})


def pay(order_no, user, *, code='', adapter=None):
    item = prepare(order_no, user)
    result = spend.execute(item.attempt_id, code=code, adapter=adapter, apply=fulfill)
    if result.status == 'unknown':
        OrderCheckout.objects.filter(pk=item.pk).update(status='unknown')
        OrderSurcharge.objects.filter(pk=item.surcharge_id).update(status='unknown')
    item.refresh_from_db()
    from apps.payments.models import Payment
    payment = Payment.objects.filter(order=item.order, status='paid').first()
    wallet = result.wallet
    wallet.refresh_from_db()
    return {'order_no': order_no, 'payment_no': payment.payment_no if payment else None,
        'status': checkout_data(item.order)['payment_status'], 'amount': format(item.total_amount, '.2f'),
        'balance': format(wallet.balance, '.2f'), 'checkout': checkout_data(item.order),
        'wechat_coin_units': result.source_snapshot['coin_units'],
        'wechat_coin_units_per_yuan': result.source_snapshot['scale'],
        'wechat_coin_diamonds': format(Decimal(result.source_snapshot['coin_amount']) * 10, '.1f')}
