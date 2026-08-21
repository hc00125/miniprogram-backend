import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from apps.common.platform_context import current_client_platform
from apps.payments.models import Payment, Refund
from apps.wallet.models import ClientWallet, ClientWalletLedger, RechargeOrder

from .models import OrderCommissionOverride


ZERO = Decimal('0.00')
HUNDRED = Decimal('100.00')
CENT = Decimal('0.01')
AUTO_REASON_PREFIX = '[channel-fee-auto]'


def _env_percent(name, default):
    raw = os.environ.get(name, str(default))
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        value = Decimal(str(default))
    return min(HUNDRED, max(ZERO, value)).quantize(CENT, rounding=ROUND_HALF_UP)


def platform_fee_percent(platform):
    platform = str(platform or '').strip().lower()
    if platform in {'ios', 'iphone', 'ipad', 'ipod'}:
        # 费率政策可能调整，因此只给保守默认值，不把政策数字写死进业务公式。
        return _env_percent('WECHAT_VIRTUALPAY_IOS_FEE_PERCENT', '15.00')
    return _env_percent('WECHAT_VIRTUALPAY_OTHER_FEE_PERCENT', '1.00')


def target_net_margin_percent():
    return _env_percent('CLUB_TARGET_NET_MARGIN_PERCENT', '15.00')


def _qmoney(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def _qpercent(value):
    return min(HUNDRED, max(ZERO, Decimal(str(value or 0)))).quantize(CENT, rounding=ROUND_HALF_UP)


def _recharge_fee_map(profile):
    result = {}
    queryset = RechargeOrder.objects.filter(
        profile=profile,
        status=RechargeOrder.STATUS_CREDITED,
    ).only('recharge_no', 'notify_payload')
    fallback = platform_fee_percent('other')
    for recharge in queryset.iterator():
        payload = dict(recharge.notify_payload or {})
        fee = payload.get('platform_fee_percent')
        try:
            result[recharge.recharge_no] = _qpercent(fee if fee is not None else fallback)
        except (InvalidOperation, TypeError, ValueError):
            result[recharge.recharge_no] = fallback
    return result


def _refund_restore_map(profile):
    result = {}
    queryset = Refund.objects.filter(
        order__boss_user__client_profile=profile,
        status=Refund.STATUS_SUCCEEDED,
    ).select_related('payment')
    for refund in queryset.iterator():
        payment = refund.payment
        if not payment or not payment.amount:
            continue
        payload = dict(payment.notify_payload or {})
        allocated = _qmoney(payload.get('platform_fee_amount_yuan') or 0)
        if allocated <= ZERO:
            continue
        ratio = min(Decimal('1'), _qmoney(refund.amount) / _qmoney(payment.amount))
        result[refund.refund_no] = _qmoney(allocated * ratio)
    return result


def wallet_fee_allocation_for_payment(profile, payment):
    """Replay the wallet ledger and allocate remaining channel cost proportionally.

    Every recharge stores its acquisition-channel fee in RechargeOrder.notify_payload.
    A later balance payment consumes the weighted fee reserve in the same proportion
    as it consumes wallet value.  Mixed iOS/non-iOS recharges therefore keep their
    original economic cost even if the customer changes device before spending.
    """
    wallet = ClientWallet.objects.filter(profile=profile).first()
    if not wallet:
        return ZERO, ZERO

    recharge_fees = _recharge_fee_map(profile)
    refund_restores = _refund_restore_map(profile)
    baseline_fee = platform_fee_percent('other')
    running_balance = ZERO
    fee_reserve = ZERO
    current_allocated = None
    current_spend = _qmoney(payment.amount)

    entries = ClientWalletLedger.objects.filter(wallet=wallet).order_by('id')
    for entry in entries.iterator():
        if entry.entry_type in ClientWalletLedger.INTERNAL_ENTRY_TYPES:
            continue
        amount = _qmoney(entry.amount)

        if entry.entry_type == ClientWalletLedger.TYPE_RECHARGE:
            fee_percent = recharge_fees.get(str(entry.reference_id or ''), baseline_fee)
            running_balance = _qmoney(running_balance + amount)
            fee_reserve = _qmoney(fee_reserve + amount * fee_percent / HUNDRED)
            continue

        if entry.entry_type == ClientWalletLedger.TYPE_REFUND_IN:
            running_balance = _qmoney(running_balance + amount)
            fee_reserve = _qmoney(fee_reserve + refund_restores.get(str(entry.reference_id or ''), ZERO))
            continue

        if amount < ZERO:
            spend = min(-amount, running_balance) if running_balance > ZERO else ZERO
            allocated = ZERO
            if spend > ZERO and fee_reserve > ZERO:
                allocated = _qmoney(fee_reserve * spend / running_balance)
                allocated = min(allocated, fee_reserve)
            if (
                entry.entry_type == ClientWalletLedger.TYPE_ORDER_PAYMENT
                and str(entry.reference_id or '') == payment.payment_no
            ):
                current_allocated = allocated
                current_spend = spend or current_spend
            running_balance = _qmoney(max(ZERO, running_balance - spend))
            fee_reserve = _qmoney(max(ZERO, fee_reserve - allocated))
            continue

        # Positive non-recharge adjustments add spendable value but no payment fee.
        running_balance = _qmoney(running_balance + amount)

    if current_allocated is None:
        actual_balance = _qmoney(wallet.balance)
        if actual_balance > ZERO and fee_reserve > ZERO:
            current_allocated = _qmoney(fee_reserve * min(current_spend, actual_balance) / actual_balance)
        else:
            current_allocated = ZERO

    fee_percent = ZERO
    if current_spend > ZERO:
        fee_percent = _qpercent(current_allocated * HUNDRED / current_spend)
    return current_allocated, fee_percent


def _payment_fee(order, payment):
    payload = dict(payment.notify_payload or {})

    if payment.channel == 'balance':
        profile = getattr(order.boss_user, 'client_profile', None)
        if profile:
            amount, percent = wallet_fee_allocation_for_payment(profile, payment)
            return amount, percent, 'wallet-weighted'

    if payment.channel == 'wechat_virtual':
        platform = payload.get('client_platform') or current_client_platform()
        percent = platform_fee_percent(platform)
        amount = _qmoney(_qmoney(payment.amount) * percent / HUNDRED)
        return amount, percent, f'wechat-virtual:{platform}'

    percent = platform_fee_percent('other')
    amount = _qmoney(_qmoney(payment.amount) * percent / HUNDRED)
    return amount, percent, f'fallback:{payment.channel}'


def protect_order_margin(order):
    """Apply a fee-aware commission override before PlayerEarning is created."""
    payment = order.payments.filter(status='paid').order_by('-paid_at', '-id').first()
    if not payment:
        return None

    fee_amount, fee_percent, source = _payment_fee(order, payment)
    target = target_net_margin_percent()
    effective_rate = _qpercent(target + fee_percent)

    payload = dict(payment.notify_payload or {})
    payload['platform_fee_amount_yuan'] = str(fee_amount)
    payload['platform_fee_percent'] = str(fee_percent)
    payload['target_net_margin_percent'] = str(target)
    payload['margin_protection_source'] = source
    if payment.channel == 'wechat_virtual' and not payload.get('client_platform'):
        payload['client_platform'] = current_client_platform()
    payment.notify_payload = payload
    payment.save(update_fields=['notify_payload'])

    existing = OrderCommissionOverride.objects.filter(order=order).first()
    if existing and not str(existing.reason or '').startswith(AUTO_REASON_PREFIX):
        # 人工覆盖属于显式经营决策，自动利润保护不越权覆盖。
        return existing

    reason = (
        f'{AUTO_REASON_PREFIX} 目标净毛利{target}% + 渠道成本{fee_percent}% '
        f'({source})'
    )[:300]
    override, _created = OrderCommissionOverride.objects.update_or_create(
        order=order,
        defaults={
            'commission_rate': effective_rate,
            'reason': reason,
            'created_by': None,
        },
    )
    return override
