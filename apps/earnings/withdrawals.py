import uuid
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import PlayerEarning, PlayerWallet, WalletLedger, Withdrawal, WithdrawalAllocation
from .settlements import release_due_earnings
from .wallet import ZERO, get_earnings_config, get_or_lock_wallet, qmoney, write_ledger


def generate_withdrawal_no():
    return f"WD{timezone.localtime().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:8].upper()}"


@transaction.atomic
def create_withdrawal(player, amount, payment_method, account_name, account_no, request_note=''):
    release_due_earnings(player=player)
    amount = qmoney(amount)
    config = get_earnings_config()
    # EarningsConfig.min_withdrawal_amount 已经以鱼干为单位，不能再次按汇率换算。
    min_fish = qmoney(config.min_withdrawal_amount)
    if amount < min_fish:
        raise ValidationError({'amount': f'最低提现金额为 {min_fish} 鱼干'})
    if not account_name or not str(account_name).strip():
        raise ValidationError({'account_name': '请填写收款人姓名'})
    if not account_no or not str(account_no).strip():
        raise ValidationError({'account_no': '请填写收款账号'})

    wallet = get_or_lock_wallet(player)
    if wallet.debt_balance > ZERO:
        raise ValidationError({
            'detail': f'当前有 {wallet.debt_balance} 鱼干待抵扣，请等待后续工资抵扣或联系管理员处理后再提现'
        })
    if wallet.available_balance < amount:
        raise ValidationError({'amount': '可提现鱼干不足'})

    withdrawal = Withdrawal.objects.create(
        withdrawal_no=generate_withdrawal_no(),
        player=player,
        wallet=wallet,
        amount=amount,
        payment_method=payment_method,
        account_name=str(account_name).strip(),
        account_no=str(account_no).strip(),
        request_note=(request_note or '')[:300],
    )

    remaining = amount
    earnings = list(
        PlayerEarning.objects
        .select_for_update()
        .filter(
            player=player,
            status=PlayerEarning.STATUS_AVAILABLE,
            available_amount__gt=ZERO,
        )
        .order_by('available_at', 'id')
    )
    for earning in earnings:
        if remaining <= ZERO:
            break
        allocated = min(qmoney(earning.available_amount), remaining)
        earning.available_amount = qmoney(earning.available_amount - allocated)
        earning.withdrawing_amount = qmoney(earning.withdrawing_amount + allocated)
        earning.save(update_fields=['available_amount', 'withdrawing_amount', 'updated_at'])
        WithdrawalAllocation.objects.create(
            withdrawal=withdrawal,
            earning=earning,
            amount=allocated,
        )
        remaining = qmoney(remaining - allocated)

    # 奖励/补发通过 WalletAdjustment 直接进入可提现余额，不对应某一条订单工资。
    # 因此 remaining 可以是合法的“非订单工资”部分；钱包余额与不可篡改流水
    # 仍是财务总账，WithdrawalAllocation 只负责追踪来自订单工资的部分。
    wallet.available_balance = qmoney(wallet.available_balance - amount)
    wallet.withdrawing_balance = qmoney(wallet.withdrawing_balance + amount)
    wallet.save(update_fields=['available_balance', 'withdrawing_balance', 'updated_at'])
    write_ledger(
        wallet,
        WalletLedger.TYPE_WITHDRAWAL_FROZEN,
        WalletLedger.BUCKET_AVAILABLE,
        -amount,
        reference_type='withdrawal',
        reference_id=withdrawal.withdrawal_no,
        note=(
            '提交提现申请，从可提现余额转出'
            if remaining <= ZERO
            else f'提交提现申请，从可提现余额转出；其中 {remaining} 鱼干来自奖励/补发调整'
        ),
    )
    write_ledger(
        wallet,
        WalletLedger.TYPE_WITHDRAWAL_FROZEN,
        WalletLedger.BUCKET_WITHDRAWING,
        amount,
        reference_type='withdrawal',
        reference_id=withdrawal.withdrawal_no,
        note='提现申请已冻结对应鱼干',
    )
    return withdrawal


@transaction.atomic
def approve_withdrawal(withdrawal, operator=None):
    withdrawal = Withdrawal.objects.select_for_update().get(pk=withdrawal.pk)
    if withdrawal.status != Withdrawal.STATUS_PENDING_REVIEW:
        raise ValidationError({'detail': '只有待审核提现可以通过审核'})
    withdrawal.status = Withdrawal.STATUS_PENDING_PAYMENT
    withdrawal.reviewed_by = operator if getattr(operator, 'is_authenticated', False) else None
    withdrawal.reviewed_at = timezone.now()
    withdrawal.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'updated_at'])
    return withdrawal


@transaction.atomic
def mark_withdrawal_paid(withdrawal, operator=None):
    withdrawal = (
        Withdrawal.objects
        .select_for_update()
        .select_related('wallet', 'player')
        .get(pk=withdrawal.pk)
    )
    if withdrawal.status != Withdrawal.STATUS_PENDING_PAYMENT:
        raise ValidationError({'detail': '只有待打款提现可以标记为已打款'})
    if not withdrawal.transfer_no.strip():
        raise ValidationError({'detail': '请先填写转账流水号'})

    wallet = PlayerWallet.objects.select_for_update().get(pk=withdrawal.wallet_id)
    if wallet.withdrawing_balance < withdrawal.amount:
        raise ValidationError({'detail': '提现中余额不足，不能确认打款'})

    allocations = list(
        WithdrawalAllocation.objects.filter(withdrawal=withdrawal).order_by('id')
    )
    for allocation in allocations:
        earning = PlayerEarning.objects.select_for_update().get(pk=allocation.earning_id)
        if earning.withdrawing_amount < allocation.amount:
            raise ValidationError({'detail': '提现分配明细异常，不能确认打款'})
        earning.withdrawing_amount = qmoney(earning.withdrawing_amount - allocation.amount)
        earning.withdrawn_amount = qmoney(earning.withdrawn_amount + allocation.amount)
        earning.save(update_fields=['withdrawing_amount', 'withdrawn_amount', 'updated_at'])

    amount = qmoney(withdrawal.amount)
    wallet.withdrawing_balance = qmoney(wallet.withdrawing_balance - amount)
    wallet.withdrawn_total = qmoney(wallet.withdrawn_total + amount)
    wallet.save(update_fields=['withdrawing_balance', 'withdrawn_total', 'updated_at'])

    withdrawal.status = Withdrawal.STATUS_PAID
    withdrawal.paid_by = operator if getattr(operator, 'is_authenticated', False) else None
    withdrawal.paid_at = timezone.now()
    if not withdrawal.reviewed_at:
        withdrawal.reviewed_at = withdrawal.paid_at
        withdrawal.reviewed_by = withdrawal.paid_by
    withdrawal.save(update_fields=[
        'status', 'paid_by', 'paid_at', 'reviewed_by', 'reviewed_at', 'updated_at'
    ])

    write_ledger(
        wallet,
        WalletLedger.TYPE_WITHDRAWAL_PAID,
        WalletLedger.BUCKET_WITHDRAWING,
        -amount,
        reference_type='withdrawal',
        reference_id=withdrawal.withdrawal_no,
        note=f'财务已打款，流水号 {withdrawal.transfer_no}',
        operator=operator,
    )
    write_ledger(
        wallet,
        WalletLedger.TYPE_WITHDRAWAL_PAID,
        WalletLedger.BUCKET_WITHDRAWN,
        amount,
        reference_type='withdrawal',
        reference_id=withdrawal.withdrawal_no,
        note='提现已完成',
        operator=operator,
    )
    return withdrawal


@transaction.atomic
def return_withdrawal(withdrawal, reason, operator=None, failed=False):
    withdrawal = Withdrawal.objects.select_for_update().select_related('wallet').get(pk=withdrawal.pk)
    if withdrawal.status not in {
        Withdrawal.STATUS_PENDING_REVIEW,
        Withdrawal.STATUS_PENDING_PAYMENT,
    }:
        raise ValidationError({'detail': '当前提现状态不能退回'})

    wallet = PlayerWallet.objects.select_for_update().get(pk=withdrawal.wallet_id)
    allocations = list(WithdrawalAllocation.objects.filter(withdrawal=withdrawal).order_by('id'))
    for allocation in allocations:
        earning = PlayerEarning.objects.select_for_update().get(pk=allocation.earning_id)
        if earning.withdrawing_amount < allocation.amount:
            raise ValidationError({'detail': '提现分配明细异常，不能退回'})
        earning.withdrawing_amount = qmoney(earning.withdrawing_amount - allocation.amount)
        earning.available_amount = qmoney(earning.available_amount + allocation.amount)
        earning.save(update_fields=['withdrawing_amount', 'available_amount', 'updated_at'])

    amount = qmoney(withdrawal.amount)
    if wallet.withdrawing_balance < amount:
        raise ValidationError({'detail': '提现中余额不足，不能退回'})
    wallet.withdrawing_balance = qmoney(wallet.withdrawing_balance - amount)
    wallet.available_balance = qmoney(wallet.available_balance + amount)
    wallet.save(update_fields=['withdrawing_balance', 'available_balance', 'updated_at'])

    withdrawal.status = Withdrawal.STATUS_FAILED if failed else Withdrawal.STATUS_REJECTED
    withdrawal.reject_reason = (reason or ('打款失败' if failed else '财务驳回'))[:300]
    withdrawal.reviewed_by = operator if getattr(operator, 'is_authenticated', False) else None
    withdrawal.reviewed_at = timezone.now()
    withdrawal.save(update_fields=[
        'status', 'reject_reason', 'reviewed_by', 'reviewed_at', 'updated_at'
    ])

    write_ledger(
        wallet,
        WalletLedger.TYPE_WITHDRAWAL_REFUNDED,
        WalletLedger.BUCKET_WITHDRAWING,
        -amount,
        reference_type='withdrawal',
        reference_id=withdrawal.withdrawal_no,
        note=withdrawal.reject_reason,
        operator=operator,
    )
    write_ledger(
        wallet,
        WalletLedger.TYPE_WITHDRAWAL_REFUNDED,
        WalletLedger.BUCKET_AVAILABLE,
        amount,
        reference_type='withdrawal',
        reference_id=withdrawal.withdrawal_no,
        note='提现金额已退回可提现余额',
        operator=operator,
    )
    return withdrawal
