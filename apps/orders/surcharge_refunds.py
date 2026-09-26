"""Atomic full refund of a paid waiting public checkout (no external call).

Same boss cancellation eligibility as the existing order API. Other refund
scopes remain explicitly manual until their own orchestration is implemented.
"""
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from .models import Order, OrderStatusLog
from .surcharge_models import OrderCheckout, OrderCheckoutRefund


def fail(code='SURCHARGE_REFUND_REQUIRES_REVIEW'):
    detail = ('加价订单此类售后请联系客服处理' if code == 'SURCHARGE_REFUND_REQUIRES_REVIEW'
              else '加价付款结果待确认，请查询原订单，不要重复付款或退款')
    raise ValidationError({'code': code, 'detail': detail})


@transaction.atomic
def cancel_unpaid_checkout(order):
    from apps.wallet.spend_service import cancel_prepared
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.paid:
        fail()
    from .payment_deadlines import _local_coin_checkout_needs_confirmation
    if _local_coin_checkout_needs_confirmation(order):
        fail('SURCHARGE_PAYMENT_PENDING')
    checkout = OrderCheckout.objects.select_for_update().filter(order=order).first()
    if not checkout or not checkout.surcharge_id:
        fail()
    if checkout.attempt_id:
        try:
            cancel_prepared(checkout.attempt_id)
        except ValidationError:
            fail('SURCHARGE_PAYMENT_PENDING')
    else:
        from .surcharge_models import OrderSurcharge
        OrderSurcharge.objects.filter(pk=checkout.surcharge_id, status='created').update(status='cancelled')
    checkout.status = 'failed'
    checkout.save(update_fields=['status'])


@transaction.atomic
def cancel_checkout(order, *, reason='', operator=None):
    from apps.payments.models import Payment, Refund
    from apps.payments.services import generate_refund_no
    from apps.wallet.services import get_or_lock_wallet, write_wallet_ledger, refund_balance_payment
    from apps.wallet.models import ClientWalletLedger
    from apps.earnings.models import PlayerEarning
    from .surcharge_lifecycle import paid_sources
    order = Order.objects.select_for_update().get(pk=order.pk)
    checkout = OrderCheckout.objects.select_for_update().filter(order=order).first()
    if not checkout or not checkout.surcharge_id:
        fail()
    existing = OrderCheckoutRefund.objects.filter(checkout=checkout).first()
    if existing:
        if order.status != Order.STATUS_CANCELLED:
            fail()
        return existing
    if order.timer_started_at or order.status not in (Order.STATUS_WAITING, Order.STATUS_PENDING_PAYMENT):
        fail()
    if not order.paid:
        fail('SURCHARGE_PAYMENT_PENDING')
    try:
        rows = paid_sources(order)
    except ValidationError:
        fail()
    if len(rows) != 1 or rows[0].pk != checkout.surcharge_id or checkout.status != 'paid':
        fail()
    surcharge = rows[0]
    attempt = surcharge.attempt
    if checkout.attempt_id != attempt.pk or checkout.total_amount != attempt.amount:
        fail()
    # No generated wages exist before service. Do not invent partial-work policy.
    if PlayerEarning.objects.filter(order=order).exists() or order.renewal_orders.filter(paid=True).exists():
        fail()
    payment = Payment.objects.select_for_update().filter(order=order, status='paid').first()
    if (not payment or payment.amount != checkout.base_amount or payment.channel != 'balance'
            or payment.notify_payload.get('checkout_attempt') != attempt.external_id
            or Refund.objects.filter(payment=payment).exists()):
        fail()
    wallet = get_or_lock_wallet(order.boss_user.client_profile)
    source = attempt.source_snapshot
    coin = Decimal(source['coin_amount'])
    local = Decimal(source['local_amount'])
    scale = int(source['scale'])
    units = source['coin_units']
    if coin + local != checkout.total_amount or coin * scale != units:
        fail()
    # Split source units, not yuan with int() truncation. Any sub-unit yuan
    # belongs to the already captured cash share, never to fabricated coin.
    base_units = min(units, int(checkout.base_amount * scale))
    base_coin = Decimal(base_units) / scale
    bonus_coin = coin - base_coin
    if bonus_coin > checkout.surcharge_amount or bonus_coin * scale != (units - base_units):
        fail()
    # Exact funding order is audited explicitly; never relabel coin as cash.
    refund = Refund.objects.create(refund_no=generate_refund_no(), payment=payment, order=order,
        amount=checkout.base_amount, reason=reason or '订单取消', status=Refund.STATUS_PENDING,
        created_by=operator if getattr(operator, 'is_authenticated', False) else None)
    refund_no = 'CR' + attempt.external_id[2:]
    payload = {'openid': attempt.request_payload['openid'], 'env': attempt.request_payload['env'],
        'user_ip': attempt.request_payload['user_ip'], 'pay_order_id': attempt.external_id,
        'order_id': refund_no, 'amount': units}
    bundle = OrderCheckoutRefund.objects.create(checkout=checkout, base_refund=refund,
        refund_no=refund_no, surcharge_amount=checkout.surcharge_amount,
        source_snapshot={'original': source, 'base_coin_yuan': str(base_coin),
            'surcharge_coin_yuan': str(bonus_coin), 'allocation_order': 'base_then_surcharge'},
        request_payload=payload, remote_status='prepared' if units else 'not_required')
    refund_payload = {'checkout_refund_id': bundle.pk, 'wechat_coin_refund_units': int(base_coin * scale),
        'wechat_coin_units_per_yuan': scale, 'wechat_coin_refund_status': 'pending' if units else 'not_required'}
    Refund.objects.filter(pk=refund.pk).update(status=Refund.STATUS_SUCCEEDED,
        notify_payload=refund_payload, third_refund_no=refund.refund_no)
    refund.refresh_from_db()
    refund_balance_payment(refund)
    wallet.refresh_from_db()
    write_wallet_ledger(wallet, ClientWalletLedger.TYPE_REFUND_IN, checkout.surcharge_amount,
        reference_type='checkout_surcharge_refund', reference_id=refund_no,
        note=f'原单 {order.order_no} 独立加价全额退款')
    surcharge.status = 'refunded'
    surcharge.refunded_diamonds = surcharge.amount_diamonds
    surcharge.save(update_fields=['status', 'refunded_diamonds', 'updated_at'])
    payment.status = 'refunded'
    payment.save(update_fields=['status', 'updated_at'])
    old_status = order.status
    order.status = Order.STATUS_CANCELLED
    order.canceled_at = timezone.now()
    order.cancel_reason = reason or '订单取消'
    order.save(update_fields=['status', 'canceled_at', 'cancel_reason'])
    OrderStatusLog.objects.create(order=order, from_status=old_status, to_status=order.status,
        operator=operator, reason=reason or '原单与整单加价一并退款到钱包')
    # This scope has no wages, but keep original VIP source accounting precise.
    from apps.accounts.vip import record_successful_refund
    record_successful_refund(refund, operator=operator)
    return bundle
