from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .diamonds import format_diamonds
from .models import ClientWallet, ClientWalletLedger, RechargeOrder


ZERO = Decimal('0.00')
HUNDRED = Decimal('100.00')
CENT = Decimal('0.01')
COIN_MODE = 'short_series_coin'


def qmoney(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def normalize_platform(value):
    value = str(value or '').strip().lower()
    if value in {'ios', 'iphone', 'ipad', 'ipod'}:
        return 'ios'
    return value or 'other'


def _percent(value):
    try:
        parsed = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        parsed = ZERO
    return min(HUNDRED, max(ZERO, parsed)).quantize(CENT, rounding=ROUND_HALF_UP)


def standalone_ios_coin_recharge(recharge):
    payload = dict(recharge.notify_payload or {})
    return (
        payload.get('mode') == COIN_MODE
        and normalize_platform(payload.get('client_platform')) == 'ios'
        and not str(getattr(recharge, 'checkout_order_no', '') or payload.get('checkout_order_no') or '').strip()
    )


def configured_wallet_credit(amount, platform, fee_percent=None):
    """Return wallet value credited for a standalone recharge.

    Android/other platforms keep the published 1 RMB = 10 diamonds rule. Standalone
    iOS wallet recharge passes the configured virtual-payment fee to the customer by
    reducing credited diamonds. Order-linked instant checkout is intentionally not
    handled here because it must still credit exactly the order amount.
    """
    amount = qmoney(amount)
    if normalize_platform(platform) != 'ios':
        return amount
    if fee_percent is None:
        from apps.earnings.platform_fees import platform_fee_percent

        fee_percent = platform_fee_percent('ios')
    fee = _percent(fee_percent)
    return qmoney(amount * (HUNDRED - fee) / HUNDRED)


def recharge_credit_amount(recharge):
    payload = dict(recharge.notify_payload or {})
    explicit = payload.get('credited_amount_yuan')
    if explicit is not None:
        try:
            return qmoney(explicit)
        except (InvalidOperation, TypeError, ValueError):
            pass
    if not standalone_ios_coin_recharge(recharge):
        return qmoney(recharge.amount)
    settlement = payload.get('estimated_settlement_yuan')
    if settlement is not None:
        try:
            return qmoney(settlement)
        except (InvalidOperation, TypeError, ValueError):
            pass
    fee = payload.get('charged_platform_fee_percent', payload.get('platform_fee_percent'))
    return configured_wallet_credit(recharge.amount, 'ios', fee)


def recharge_display_fee_percent(recharge):
    payload = dict(recharge.notify_payload or {})
    return str(payload.get('charged_platform_fee_percent') or payload.get('platform_fee_percent') or '0.00')


def decorate_wallet_recharge_payload(payload, recharge):
    """Expose the actual diamonds that a standalone wallet recharge will credit."""
    result = dict(payload or {})
    credited = recharge_credit_amount(recharge)
    stored = dict(recharge.notify_payload or {})
    result.update({
        'pay_amount_yuan': str(qmoney(recharge.amount)),
        'diamonds': format_diamonds(credited),
        'credited_amount_yuan': str(credited),
        'credited_diamonds': format_diamonds(credited),
        'platform_fee_percent': recharge_display_fee_percent(recharge),
        'estimated_platform_fee_yuan': stored.get('estimated_platform_fee_yuan'),
        'estimated_settlement_yuan': stored.get('estimated_settlement_yuan'),
        'fee_deducted_from_credit': bool(stored.get('fee_deducted_from_credit') or standalone_ios_coin_recharge(recharge)),
    })
    return result


@receiver(post_save, sender=RechargeOrder, dispatch_uid='wallet_apply_ios_net_recharge_credit')
def apply_ios_net_recharge_credit(sender, instance, **kwargs):
    """Convert a newly credited standalone iOS recharge from gross to net credit.

    The core recharge service first records the gross paid amount. This signal runs
    immediately after that credit, rewrites the same recharge ledger entry to the
    net amount and adjusts wallet totals in the same transaction. No extra visible
    debit line is created. Existing historical credited recharges are untouched.
    """
    if instance.status != RechargeOrder.STATUS_CREDITED or not instance.credited_at:
        return
    if not standalone_ios_coin_recharge(instance):
        return

    initial = dict(instance.notify_payload or {})
    if initial.get('fee_deducted_from_credit'):
        return

    gross = qmoney(instance.amount)
    net = recharge_credit_amount(instance)
    if net <= ZERO or net >= gross:
        return

    with transaction.atomic():
        recharge = RechargeOrder.objects.select_for_update().get(pk=instance.pk)
        payload = dict(recharge.notify_payload or {})
        if payload.get('fee_deducted_from_credit') or not standalone_ios_coin_recharge(recharge):
            return

        gross = qmoney(recharge.amount)
        net = recharge_credit_amount(recharge)
        if net <= ZERO or net >= gross:
            return

        wallet = ClientWallet.objects.select_for_update().filter(profile_id=recharge.profile_id).first()
        if not wallet:
            return
        ledger = (
            ClientWalletLedger.objects
            .select_for_update()
            .filter(
                wallet=wallet,
                entry_type=ClientWalletLedger.TYPE_RECHARGE,
                reference_id=recharge.recharge_no,
            )
            .first()
        )
        if not ledger:
            return

        current = qmoney(ledger.amount)
        deduction = max(ZERO, qmoney(current - net))
        if deduction > ZERO:
            ledger.amount = net
            ledger.balance_after = qmoney(ledger.balance_after - deduction)
            ledger.note = (
                f'iOS充值单 {recharge.recharge_no} 实付¥{gross:.2f}，'
                f'扣除渠道费¥{qmoney(gross - net):.2f}，实际到账💎{format_diamonds(net)}'
            )[:500]
            ledger.save(update_fields=['amount', 'balance_after', 'note'])

            wallet.balance = qmoney(wallet.balance - deduction)
            wallet.recharged_total = qmoney(max(ZERO, wallet.recharged_total - deduction))
            wallet.save(update_fields=['balance', 'recharged_total', 'updated_at'])

        charged_fee = str(payload.get('charged_platform_fee_percent') or payload.get('platform_fee_percent') or '0.00')
        payload['charged_platform_fee_percent'] = charged_fee
        # Downstream order-margin protection replays this field. The iOS fee has
        # already been borne by reduced recharge credit, so set the remaining fee
        # reserve to zero to avoid charging the same channel cost a second time.
        payload['platform_fee_percent'] = '0.00'
        payload['credited_amount_yuan'] = str(net)
        payload['credited_diamonds'] = format_diamonds(net)
        payload['fee_deducted_from_credit'] = True
        RechargeOrder.objects.filter(pk=recharge.pk).update(notify_payload=payload)
