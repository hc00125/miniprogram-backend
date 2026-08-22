from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.orders.models import Order
from apps.orders.payment_deadlines import ensure_payment_window_open
from apps.payments.models import Payment
from apps.payments.services import (
    ensure_order_owner,
    generate_payment_no,
    get_order_amount,
    mark_payment_paid,
)
from apps.payments.virtualpay import VIRTUAL_CHANNEL, VIRTUAL_MODE_GOODS, query_virtual_payment

from .coin_sync import execute_coin_spend, prepare_coin_spend
from .models import ClientWalletLedger, RechargeOrder
from .services import (
    BALANCE_CHANNEL,
    BALANCE_SCENE,
    ZERO,
    get_or_lock_wallet,
    qmoney,
    write_wallet_ledger,
)


def _reserved_checkout_amount(profile, *, exclude_order_no=''):
    """Return wallet value already paid by WeChat and reserved for other orders.

    A short_series_coin checkout credits the wallet before finalize consumes the
    same amount. Until that business order is paid or cancelled, the credited
    value is not generic spendable balance and must not be moved to another
    order.
    """
    pending_order_nos = (
        Order.objects
        .filter(
            boss_user=profile.user,
            paid=False,
            status=Order.STATUS_PENDING_PAYMENT,
        )
        .exclude(order_no=str(exclude_order_no or ''))
        .values_list('order_no', flat=True)
    )
    total = (
        RechargeOrder.objects
        .filter(
            profile=profile,
            status=RechargeOrder.STATUS_CREDITED,
            checkout_order_no__in=pending_order_nos,
        )
        .aggregate(total=Sum('amount'))
        .get('total')
    )
    return qmoney(total or ZERO)


def pay_order_with_coin_aware_balance(order_no, user, code='', user_ip='127.0.0.1'):
    """Pay an order with the local wallet and mirror coin-backed spend to XPay.

    Historical/manual wallet value remains fully compatible and does not require
    a wx.login code. Only the share traceable to successful short_series_coin
    recharges is deducted through /xpay/currency_pay. Order-linked credited
    recharges stay reserved for their original order until finalize completes.
    """
    order = ensure_payment_window_open(order_no, user)
    ensure_order_owner(order, user)

    profile = getattr(user, 'client_profile', None)
    if not profile:
        raise ValidationError({'detail': '请先微信登录'})

    now = timezone.now()
    virtual_paying = list(
        Payment.objects
        .filter(
            order__order_no=order_no,
            channel=VIRTUAL_CHANNEL,
            scene=VIRTUAL_MODE_GOODS,
            status='paying',
        )
        .order_by('-created_at')
    )
    verified_unpaid_pks = set()
    for payment in virtual_paying:
        if not payment.expires_at or payment.expires_at > now:
            raise ValidationError({'detail': '存在进行中的微信支付，请完成或等待其过期后再用余额支付'})
        synced = query_virtual_payment(payment.payment_no, user)
        if synced.status == 'paid' or getattr(synced.order, 'paid', False):
            raise ValidationError({'detail': '订单已通过微信支付，请刷新订单状态'})
        verified_unpaid_pks.add(payment.pk)

    with transaction.atomic():
        order = (
            Order.objects
            .select_for_update(of=('self',))
            .select_related('boss_user')
            .filter(order_no=order_no)
            .first()
        )
        if not order:
            raise ValidationError({'detail': '订单不存在'})
        ensure_order_owner(order, user)
        if order.paid or order.status == Order.STATUS_COMPLETED:
            raise ValidationError({'detail': '订单已支付'})
        if order.status != Order.STATUS_PENDING_PAYMENT:
            raise ValidationError({'detail': '当前订单状态不可支付'})

        amount = get_order_amount(order)
        if amount <= ZERO:
            raise ValidationError({'detail': '订单金额不正确'})

        paying_now = list(
            Payment.objects
            .select_for_update()
            .filter(order=order, status='paying')
        )
        for payment in paying_now:
            unverified = payment.pk not in verified_unpaid_pks
            not_virtual = payment.channel != VIRTUAL_CHANNEL or payment.scene != VIRTUAL_MODE_GOODS
            unexpired = not payment.expires_at or payment.expires_at > timezone.now()
            if unverified or not_virtual or unexpired:
                raise ValidationError({'detail': '存在进行中的微信支付，请完成或等待其过期后再用余额支付'})
        if paying_now:
            Payment.objects.filter(
                pk__in=[payment.pk for payment in paying_now],
                status='paying',
            ).update(status='closed', updated_at=timezone.now())

        wallet = get_or_lock_wallet(profile)
        reserved_for_other_orders = _reserved_checkout_amount(
            profile,
            exclude_order_no=order.order_no,
        )
        available_for_this_order = qmoney(wallet.balance - reserved_for_other_orders)
        if available_for_this_order < amount:
            if reserved_for_other_orders > ZERO:
                raise ValidationError({
                    'detail': '部分钻石正在等待另一笔已付款订单确认，暂不可用于本订单，请先完成原订单确认',
                })
            raise ValidationError({'detail': '余额不足'})

        # The wallet row is locked while XPay is called. This is intentionally
        # conservative: it prevents two concurrent balance payments from using
        # the same coin-backed share. The XPay order_id is deterministic, so a
        # rare DB rollback after remote success is safely retryable.
        coin_metadata = prepare_coin_spend(profile, amount, code, user_ip)
        coin_metadata = execute_coin_spend(profile, order, coin_metadata)
        coin_metadata.pop('_session_key', None)
        coin_metadata.pop('_user_ip', None)

        payment_no = generate_payment_no()
        write_wallet_ledger(
            wallet,
            ClientWalletLedger.TYPE_ORDER_PAYMENT,
            -amount,
            reference_type='payment',
            reference_id=payment_no,
            note=f'余额支付订单 {order.order_no}',
            operator=user,
        )
        wallet.spent_total = qmoney(wallet.spent_total + amount)
        wallet.save(update_fields=['spent_total', 'updated_at'])

        payment_payload = {
            'balance_payment': True,
            **coin_metadata,
        }
        payment = Payment.objects.create(
            payment_no=payment_no,
            order=order,
            channel=BALANCE_CHANNEL,
            scene=BALANCE_SCENE,
            amount=amount,
            status='paying',
            third_order_no=payment_no,
            notify_payload=payment_payload,
        )
        payment = mark_payment_paid(payment, payment_no, payment_payload)

    return {
        'payment_no': payment.payment_no,
        'order_no': order.order_no,
        'status': payment.status,
        'amount': str(qmoney(amount)),
        'balance': str(qmoney(wallet.balance)),
        'wechat_coin_diamonds': str(coin_metadata.get('wechat_coin_diamonds') or '0.0'),
        'wechat_coin_units': int(coin_metadata.get('wechat_coin_units') or 0),
        'wechat_coin_units_per_yuan': int(coin_metadata.get('wechat_coin_units_per_yuan') or 0),
    }
