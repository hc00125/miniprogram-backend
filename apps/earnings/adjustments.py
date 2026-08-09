from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.orders.models import Order, OrderStatusLog

from .models import PlayerEarning, WalletAdjustment, WalletLedger
from .settlements import get_order_paid_revenue
from .wallet import ZERO, get_or_lock_wallet, qmoney, write_ledger


def _operator_or_none(operator):
    return operator if getattr(operator, 'is_authenticated', False) else None


def _adjustment_entry_type(adjustment):
    if adjustment.adjustment_type == WalletAdjustment.TYPE_REFUND_REVERSAL:
        return WalletLedger.TYPE_EARNING_REVERSED
    return WalletLedger.TYPE_ADMIN_ADJUSTMENT


def _write_adjustment_ledgers(wallet, adjustment, operator=None):
    common = {
        'reference_type': adjustment.reference_type or 'wallet_adjustment',
        'reference_id': adjustment.reference_id or adjustment.id,
        'note': adjustment.reason,
        'operator': operator,
    }
    entry_type = _adjustment_entry_type(adjustment)
    if adjustment.pending_delta:
        write_ledger(wallet, entry_type, WalletLedger.BUCKET_PENDING, adjustment.pending_delta, **common)
    if adjustment.available_delta:
        write_ledger(wallet, entry_type, WalletLedger.BUCKET_AVAILABLE, adjustment.available_delta, **common)
    if adjustment.debt_delta:
        write_ledger(wallet, entry_type, WalletLedger.BUCKET_DEBT, adjustment.debt_delta, **common)


@transaction.atomic
def apply_wallet_adjustment(adjustment, operator=None):
    """Apply a manual reward or penalty exactly once.

    Penalties first consume available fish and put any shortfall into debt.
    Rewards first clear debt and then become immediately withdrawable.
    Pending wages are never mutated by a manual adjustment, so every order
    earning remains reconcilable until its review period completes.
    """
    adjustment = WalletAdjustment.objects.select_for_update().select_related('player').get(pk=adjustment.pk)
    if adjustment.applied_at:
        return adjustment
    if adjustment.adjustment_type == WalletAdjustment.TYPE_REFUND_REVERSAL:
        raise ValidationError({'detail': '退款冲销只能由退款记录自动生成'})

    amount = qmoney(adjustment.amount)
    if amount <= ZERO:
        raise ValidationError({'amount': '调整鱼干必须大于0'})

    wallet = get_or_lock_wallet(adjustment.player)
    if adjustment.adjustment_type == WalletAdjustment.TYPE_REWARD:
        debt_offset = min(qmoney(wallet.debt_balance), amount)
        available_add = qmoney(amount - debt_offset)
        wallet.debt_balance = qmoney(wallet.debt_balance - debt_offset)
        wallet.available_balance = qmoney(wallet.available_balance + available_add)
        adjustment.available_delta = available_add
        adjustment.pending_delta = ZERO
        adjustment.debt_delta = -debt_offset
    elif adjustment.adjustment_type == WalletAdjustment.TYPE_PENALTY:
        available_take = min(qmoney(wallet.available_balance), amount)
        debt_add = qmoney(amount - available_take)
        wallet.available_balance = qmoney(wallet.available_balance - available_take)
        wallet.debt_balance = qmoney(wallet.debt_balance + debt_add)
        adjustment.available_delta = -available_take
        adjustment.pending_delta = ZERO
        adjustment.debt_delta = debt_add
    else:
        raise ValidationError({'adjustment_type': '不支持的调整类型'})

    wallet.save(update_fields=['available_balance', 'debt_balance', 'updated_at'])
    adjustment.created_by = adjustment.created_by or _operator_or_none(operator)
    adjustment.applied_at = timezone.now()
    adjustment.save(update_fields=[
        'available_delta', 'pending_delta', 'debt_delta',
        'created_by', 'applied_at',
    ])
    _write_adjustment_ledgers(wallet, adjustment, operator=operator)
    return adjustment


@transaction.atomic
def create_and_apply_adjustment(
    *, player, adjustment_type, amount, reason,
    order=None, evidence_url='', operator=None,
):
    if adjustment_type not in {WalletAdjustment.TYPE_PENALTY, WalletAdjustment.TYPE_REWARD}:
        raise ValidationError({'adjustment_type': '后台只能手动创建罚款或奖励'})
    adjustment = WalletAdjustment.objects.create(
        player=player,
        order=order,
        adjustment_type=adjustment_type,
        amount=qmoney(amount),
        reason=(reason or '').strip(),
        evidence_url=(evidence_url or '').strip(),
        created_by=_operator_or_none(operator),
    )
    if not adjustment.reason:
        raise ValidationError({'reason': '请填写奖惩原因'})
    return apply_wallet_adjustment(adjustment, operator=operator)


def _refund_root_order(refund):
    order = refund.order
    if order.order_type == Order.ORDER_TYPE_RENEWAL and order.parent_order_id:
        return order.parent_order
    return order


def _successful_refund_total(root_order):
    from apps.payments.models import Refund

    order_ids = [root_order.order_no]
    order_ids.extend(root_order.renewal_orders.values_list('order_no', flat=True))
    result = Refund.objects.filter(
        order_id__in=order_ids,
        status=Refund.STATUS_SUCCEEDED,
    ).aggregate(total=Sum('amount'))
    return qmoney(result['total'] or ZERO)


@transaction.atomic
def reverse_refund_earnings(refund, operator=None):
    """Reverse player net wages after a refund succeeds.

    Unreleased wages are removed from pending balance. Released wages are
    removed from available balance. Funds already withdrawing/withdrawn are
    not silently rewritten; the shortfall becomes debt and is offset against
    future wages. The refund reference makes every player reversal idempotent.
    """
    from apps.payments.models import Refund

    refund = (
        Refund.objects
        .select_for_update(of=('self',))
        .select_related('order__parent_order')
        .get(pk=refund.pk)
    )
    if refund.status != Refund.STATUS_SUCCEEDED:
        raise ValidationError({'detail': '只有退款成功后才能冲销陪玩工资'})

    root_order = _refund_root_order(refund)
    earnings = list(
        PlayerEarning.objects
        .select_for_update()
        .select_related('player')
        .filter(order=root_order)
        .order_by('id')
    )
    if not earnings:
        return []

    total_revenue = qmoney(get_order_paid_revenue(root_order))
    refund_amount = qmoney(refund.amount)
    if total_revenue <= ZERO or refund_amount <= ZERO:
        raise ValidationError({'detail': '退款或订单收入金额不正确，无法冲销工资'})

    full_reversal = _successful_refund_total(root_order) >= total_revenue
    results = []
    created_any = False

    for earning in earnings:
        existing = WalletAdjustment.objects.filter(
            player=earning.player,
            adjustment_type=WalletAdjustment.TYPE_REFUND_REVERSAL,
            reference_type='refund',
            reference_id=refund.refund_no,
            applied_at__isnull=False,
        ).first()
        if existing:
            results.append(existing)
            continue

        remaining_reversible = qmoney(earning.net_amount - earning.reversed_amount)
        if remaining_reversible <= ZERO:
            continue
        target = remaining_reversible if full_reversal else qmoney(
            earning.net_amount * refund_amount / total_revenue
        )
        amount = min(remaining_reversible, target)
        if amount <= ZERO:
            continue

        wallet = get_or_lock_wallet(earning.player)
        pending_take = ZERO
        if earning.status in {PlayerEarning.STATUS_PENDING, PlayerEarning.STATUS_FROZEN}:
            pending_take = min(qmoney(wallet.pending_balance), amount)
            wallet.pending_balance = qmoney(wallet.pending_balance - pending_take)

        remaining = qmoney(amount - pending_take)
        available_take = min(
            qmoney(wallet.available_balance),
            qmoney(earning.available_amount),
            remaining,
        )
        if available_take:
            wallet.available_balance = qmoney(wallet.available_balance - available_take)
            earning.available_amount = qmoney(earning.available_amount - available_take)
        remaining = qmoney(remaining - available_take)

        debt_add = remaining
        if debt_add:
            wallet.debt_balance = qmoney(wallet.debt_balance + debt_add)

        earning.reversed_amount = qmoney(earning.reversed_amount + amount)
        if earning.reversed_amount >= earning.net_amount:
            earning.status = PlayerEarning.STATUS_REVERSED
        earning.save(update_fields=['reversed_amount', 'available_amount', 'status', 'updated_at'])
        wallet.save(update_fields=['pending_balance', 'available_balance', 'debt_balance', 'updated_at'])

        adjustment = WalletAdjustment.objects.create(
            player=earning.player,
            order=root_order,
            adjustment_type=WalletAdjustment.TYPE_REFUND_REVERSAL,
            amount=amount,
            pending_delta=-pending_take,
            available_delta=-available_take,
            debt_delta=debt_add,
            reason=f'退款 {refund.refund_no} 成功，冲销订单 {root_order.order_no} 对应工资',
            reference_type='refund',
            reference_id=refund.refund_no,
            created_by=_operator_or_none(operator),
            applied_at=timezone.now(),
        )
        _write_adjustment_ledgers(wallet, adjustment, operator=operator)
        results.append(adjustment)
        created_any = True

    if created_any:
        OrderStatusLog.objects.create(
            order=root_order,
            from_status=root_order.status,
            to_status=root_order.status,
            operator=_operator_or_none(operator),
            reason=f'退款 {refund.refund_no} 成功，已同步冲销陪玩工资',
        )
    return results
