from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.orders.models import Order

from .models import OrderCommissionOverride, PlayerEarning, WalletLedger
from .wallet import ZERO, get_earnings_config, get_or_lock_wallet, qmoney, write_ledger


def get_commission_rate(order):
    try:
        override = order.commission_override
    except OrderCommissionOverride.DoesNotExist:
        override = None
    if override is not None:
        return qmoney(override.commission_rate)
    return qmoney(get_earnings_config().default_commission_rate)


def get_order_paid_revenue(order):
    base_amount = qmoney(order.total_amount if order.total_amount is not None else order.total_price_per_hour)
    renewal_amount = sum(
        (
            qmoney(item.total_amount if item.total_amount is not None else item.total_price_per_hour)
            for item in order.renewal_orders.filter(
                order_type=Order.ORDER_TYPE_RENEWAL,
                paid=True,
                status=Order.STATUS_COMPLETED,
            )
        ),
        ZERO,
    )
    return qmoney(base_amount + renewal_amount)


def split_money(total, count):
    total = qmoney(total)
    if count <= 0:
        return []
    total_cents = int((total * 100).to_integral_value(rounding=ROUND_HALF_UP))
    base_cents, remainder = divmod(total_cents, count)
    return [
        Decimal(base_cents + (1 if index < remainder else 0)) / Decimal('100')
        for index in range(count)
    ]


@transaction.atomic
def create_order_earnings(order, completed_at=None):
    order = Order.objects.select_for_update().select_related('package').get(pk=order.pk)
    if order.order_type != Order.ORDER_TYPE_NORMAL:
        return []
    if order.status != Order.STATUS_COMPLETED or not order.paid:
        return []

    order_players = list(order.order_players.select_related('player').order_by('id'))
    if not order_players:
        raise ValidationError({'detail': '订单没有陪玩阵容，不能生成工资'})

    total_revenue = get_order_paid_revenue(order)
    if total_revenue <= ZERO:
        raise ValidationError({'detail': '订单实付金额不正确，不能生成工资'})

    config = get_earnings_config()
    rate = get_commission_rate(order)
    exchange_multiplier = Decimal(str(settings.FISH_CRACKER_EXCHANGE_RATE))
    total_yugan = qmoney(total_revenue * exchange_multiplier)
    gross_amounts = split_money(total_yugan, len(order_players))
    completed_at = completed_at or order.end_time or timezone.now()
    review_until = completed_at + timedelta(days=config.review_days)
    existing = {
        item.player_id: item
        for item in PlayerEarning.objects.filter(order=order).select_related('player')
    }
    results = []

    for index, order_player in enumerate(order_players):
        if order_player.player_id in existing:
            results.append(existing[order_player.player_id])
            continue

        gross = qmoney(gross_amounts[index])
        commission = qmoney(gross * rate / Decimal('100'))
        net = qmoney(gross - commission)
        earning = PlayerEarning.objects.create(
            order=order,
            player=order_player.player,
            gross_amount=gross,
            commission_rate=rate,
            commission_amount=commission,
            net_amount=net,
            status=PlayerEarning.STATUS_PENDING,
            review_until=review_until,
        )
        wallet = get_or_lock_wallet(order_player.player)
        wallet.pending_balance = qmoney(wallet.pending_balance + net)
        wallet.save(update_fields=['pending_balance', 'updated_at'])
        write_ledger(
            wallet,
            WalletLedger.TYPE_EARNING_CREATED,
            WalletLedger.BUCKET_PENDING,
            net,
            reference_type='earning',
            reference_id=earning.id,
            note=f'订单 {order.order_no} 工资进入{config.review_days}天审核期',
        )
        results.append(earning)
    return results


def _release_earning_locked(earning, now):
    if earning.status != PlayerEarning.STATUS_PENDING or earning.review_until > now:
        return False

    wallet = get_or_lock_wallet(earning.player)
    amount = qmoney(earning.net_amount - earning.reversed_amount)
    if amount <= ZERO:
        earning.status = PlayerEarning.STATUS_REVERSED
        earning.available_at = now
        earning.available_amount = ZERO
        earning.save(update_fields=['status', 'available_at', 'available_amount', 'updated_at'])
        return True
    if wallet.pending_balance < amount:
        raise ValidationError({'detail': f'{earning.player.name}审核中余额异常，无法自动解冻'})

    wallet.pending_balance = qmoney(wallet.pending_balance - amount)
    debt_offset = min(qmoney(wallet.debt_balance), amount)
    available_amount = qmoney(amount - debt_offset)
    wallet.debt_balance = qmoney(wallet.debt_balance - debt_offset)
    wallet.available_balance = qmoney(wallet.available_balance + available_amount)
    wallet.save(update_fields=['pending_balance', 'available_balance', 'debt_balance', 'updated_at'])

    earning.status = PlayerEarning.STATUS_AVAILABLE
    earning.available_at = now
    earning.available_amount = available_amount
    earning.debt_offset_amount = qmoney(earning.debt_offset_amount + debt_offset)
    earning.save(update_fields=[
        'status', 'available_at', 'available_amount', 'debt_offset_amount', 'updated_at',
    ])

    write_ledger(
        wallet,
        WalletLedger.TYPE_EARNING_RELEASED,
        WalletLedger.BUCKET_PENDING,
        -amount,
        reference_type='earning',
        reference_id=earning.id,
        note=f'订单 {earning.order.order_no} 工资审核通过，从审核中转出',
    )
    if debt_offset:
        write_ledger(
            wallet,
            WalletLedger.TYPE_ADMIN_ADJUSTMENT,
            WalletLedger.BUCKET_DEBT,
            -debt_offset,
            reference_type='earning',
            reference_id=earning.id,
            note=f'订单 {earning.order.order_no} 工资优先抵扣待抵扣鱼干',
        )
    if available_amount:
        write_ledger(
            wallet,
            WalletLedger.TYPE_EARNING_RELEASED,
            WalletLedger.BUCKET_AVAILABLE,
            available_amount,
            reference_type='earning',
            reference_id=earning.id,
            note=f'订单 {earning.order.order_no} 工资转为可提现',
        )
    return True


@transaction.atomic
def release_due_earnings(now=None, player=None, limit=500):
    now = now or timezone.now()
    queryset = PlayerEarning.objects.filter(
        status=PlayerEarning.STATUS_PENDING,
        review_until__lte=now,
    ).order_by('review_until', 'id')
    if player is not None:
        queryset = queryset.filter(player=player)
    earning_ids = list(queryset.values_list('id', flat=True)[:limit])
    released = 0
    for earning_id in earning_ids:
        earning = (
            PlayerEarning.objects
            .select_for_update()
            .select_related('player', 'order')
            .get(pk=earning_id)
        )
        if _release_earning_locked(earning, now):
            released += 1
    return released


@transaction.atomic
def recalculate_pending_order_earnings(order, operator=None):
    order = Order.objects.select_for_update().get(pk=order.pk)
    earnings = list(
        PlayerEarning.objects
        .select_for_update()
        .select_related('player')
        .filter(order=order)
        .order_by('id')
    )
    if not earnings:
        return []
    if any(
        item.status != PlayerEarning.STATUS_PENDING
        or item.reversed_amount > ZERO
        or item.debt_offset_amount > ZERO
        or item.available_amount > ZERO
        or item.withdrawing_amount > ZERO
        or item.withdrawn_amount > ZERO
        for item in earnings
    ):
        raise ValidationError({'detail': '工资已冲销、审核或进入提现流程，不能再修改本单抽成'})

    total_revenue = get_order_paid_revenue(order)
    rate = get_commission_rate(order)
    exchange_multiplier = Decimal(str(settings.FISH_CRACKER_EXCHANGE_RATE))
    total_yugan = qmoney(total_revenue * exchange_multiplier)
    gross_amounts = split_money(total_yugan, len(earnings))

    for index, earning in enumerate(earnings):
        gross = qmoney(gross_amounts[index])
        commission = qmoney(gross * rate / Decimal('100'))
        net = qmoney(gross - commission)
        delta = qmoney(net - earning.net_amount)
        if delta:
            wallet = get_or_lock_wallet(earning.player)
            new_pending = qmoney(wallet.pending_balance + delta)
            if new_pending < ZERO:
                raise ValidationError({'detail': '调整后审核中余额不能为负数'})
            wallet.pending_balance = new_pending
            wallet.save(update_fields=['pending_balance', 'updated_at'])
            write_ledger(
                wallet,
                WalletLedger.TYPE_EARNING_ADJUSTED,
                WalletLedger.BUCKET_PENDING,
                delta,
                reference_type='earning',
                reference_id=earning.id,
                note=f'订单 {order.order_no} 抽成调整为 {rate}%',
                operator=operator,
            )
        earning.gross_amount = gross
        earning.commission_rate = rate
        earning.commission_amount = commission
        earning.net_amount = net
        earning.save(update_fields=[
            'gross_amount', 'commission_rate', 'commission_amount', 'net_amount', 'updated_at',
        ])
    return earnings


@transaction.atomic
def freeze_pending_earning(earning, reason):
    earning = PlayerEarning.objects.select_for_update().get(pk=earning.pk)
    if earning.status != PlayerEarning.STATUS_PENDING:
        raise ValidationError({'detail': '只能冻结审核中的工资'})
    earning.status = PlayerEarning.STATUS_FROZEN
    earning.freeze_reason = (reason or '管理员冻结')[:300]
    earning.save(update_fields=['status', 'freeze_reason', 'updated_at'])
    return earning


@transaction.atomic
def unfreeze_earning(earning):
    earning = PlayerEarning.objects.select_for_update().select_related('player', 'order').get(pk=earning.pk)
    if earning.status != PlayerEarning.STATUS_FROZEN:
        raise ValidationError({'detail': '当前工资未被冻结'})
    earning.status = PlayerEarning.STATUS_PENDING
    earning.freeze_reason = ''
    earning.save(update_fields=['status', 'freeze_reason', 'updated_at'])
    if earning.review_until <= timezone.now():
        _release_earning_locked(earning, timezone.now())
    return earning
