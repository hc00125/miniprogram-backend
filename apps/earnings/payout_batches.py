import uuid
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import PlayerEarning, PlayerWallet, WalletLedger, Withdrawal, WithdrawalAllocation
from .payout_batch_models import WithdrawalPayoutBatch, WithdrawalPayoutBatchItem
from .wallet import qmoney, write_ledger


def generate_payout_batch_no():
    return f"PAYOUT{timezone.localtime().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:6].upper()}"


def _operator_or_none(operator):
    return operator if getattr(operator, 'is_authenticated', False) else None


def _append_note(existing, common_note):
    existing = (existing or '').strip()
    common_note = (common_note or '').strip()
    if not common_note:
        return existing
    if not existing:
        return common_note[:500]
    if common_note in existing:
        return existing[:500]
    return f'{existing}\n{common_note}'[:500]


def _lock_withdrawals(withdrawal_ids):
    normalized_ids = sorted({int(value) for value in withdrawal_ids or []})
    if not normalized_ids:
        raise ValidationError({'detail': '请先选择提现申请'})
    withdrawals = list(
        Withdrawal.objects
        .select_for_update()
        .select_related('wallet', 'player')
        .filter(pk__in=normalized_ids)
        .order_by('id')
    )
    if len(withdrawals) != len(normalized_ids):
        raise ValidationError({'detail': '部分提现申请不存在或已变化，请刷新后重试'})
    return withdrawals


@transaction.atomic
def approve_withdrawals_batch(withdrawal_ids, operator=None, admin_note=''):
    withdrawals = _lock_withdrawals(withdrawal_ids)
    invalid = [item.withdrawal_no for item in withdrawals if item.status != Withdrawal.STATUS_PENDING_REVIEW]
    if invalid:
        raise ValidationError({'detail': f'以下提现不是待审核状态：{", ".join(invalid[:5])}'})

    now = timezone.now()
    reviewer = _operator_or_none(operator)
    for withdrawal in withdrawals:
        withdrawal.status = Withdrawal.STATUS_PENDING_PAYMENT
        withdrawal.reviewed_by = reviewer
        withdrawal.reviewed_at = now
        withdrawal.admin_note = _append_note(withdrawal.admin_note, admin_note)
        withdrawal.save(update_fields=[
            'status', 'reviewed_by', 'reviewed_at', 'admin_note', 'updated_at',
        ])
    return withdrawals


@transaction.atomic
def mark_withdrawals_paid_batch(
    withdrawal_ids,
    *,
    operator=None,
    payment_method=Withdrawal.METHOD_WECHAT,
    external_transfer_no='',
    admin_note='',
    proof_url='',
):
    valid_methods = {value for value, _ in Withdrawal.METHOD_CHOICES}
    if payment_method not in valid_methods:
        raise ValidationError({'payment_method': '付款方式不正确'})

    withdrawals = _lock_withdrawals(withdrawal_ids)
    invalid = [item.withdrawal_no for item in withdrawals if item.status != Withdrawal.STATUS_PENDING_PAYMENT]
    if invalid:
        raise ValidationError({'detail': f'以下提现不是待打款状态：{", ".join(invalid[:5])}'})
    duplicate = [
        item.withdrawal_no
        for item in withdrawals
        if WithdrawalPayoutBatchItem.objects.filter(withdrawal=item).exists()
    ]
    if duplicate:
        raise ValidationError({'detail': f'以下提现已属于付款批次：{", ".join(duplicate[:5])}'})

    wallet_totals = defaultdict(lambda: Decimal('0.00'))
    for withdrawal in withdrawals:
        wallet_totals[withdrawal.wallet_id] += qmoney(withdrawal.amount)

    wallets = {
        wallet.id: wallet
        for wallet in PlayerWallet.objects.select_for_update().filter(id__in=wallet_totals.keys())
    }
    for wallet_id, total in wallet_totals.items():
        wallet = wallets.get(wallet_id)
        if not wallet or qmoney(wallet.withdrawing_balance) < qmoney(total):
            raise ValidationError({'detail': '提现中余额不足，不能批量确认打款'})

    allocation_map = defaultdict(list)
    earning_totals = defaultdict(lambda: Decimal('0.00'))
    allocations = list(
        WithdrawalAllocation.objects
        .select_related('earning')
        .filter(withdrawal__in=withdrawals)
        .order_by('withdrawal_id', 'id')
    )
    earning_ids = sorted({item.earning_id for item in allocations})
    earnings = {
        earning.id: earning
        for earning in PlayerEarning.objects.select_for_update().filter(id__in=earning_ids)
    }
    for allocation in allocations:
        allocation_map[allocation.withdrawal_id].append(allocation)
        earning_totals[allocation.earning_id] += qmoney(allocation.amount)

    for earning_id, total in earning_totals.items():
        earning = earnings.get(earning_id)
        if not earning or qmoney(earning.withdrawing_amount) < qmoney(total):
            raise ValidationError({'detail': '提现分配明细异常，不能批量确认打款'})

    total_amount = qmoney(sum((qmoney(item.amount) for item in withdrawals), Decimal('0.00')))
    batch = WithdrawalPayoutBatch.objects.create(
        batch_no=generate_payout_batch_no(),
        payment_method=payment_method,
        item_count=len(withdrawals),
        total_amount=total_amount,
        external_transfer_no=(external_transfer_no or '').strip()[:150],
        admin_note=(admin_note or '').strip()[:500],
        proof_url=(proof_url or '').strip()[:500],
        created_by=_operator_or_none(operator),
    )

    paid_at = timezone.now()
    for withdrawal in withdrawals:
        for allocation in allocation_map.get(withdrawal.id, []):
            earning = earnings[allocation.earning_id]
            amount = qmoney(allocation.amount)
            earning.withdrawing_amount = qmoney(earning.withdrawing_amount - amount)
            earning.withdrawn_amount = qmoney(earning.withdrawn_amount + amount)
            earning.save(update_fields=['withdrawing_amount', 'withdrawn_amount', 'updated_at'])

        amount = qmoney(withdrawal.amount)
        wallet = wallets[withdrawal.wallet_id]
        wallet.withdrawing_balance = qmoney(wallet.withdrawing_balance - amount)
        wallet.withdrawn_total = qmoney(wallet.withdrawn_total + amount)
        wallet.save(update_fields=['withdrawing_balance', 'withdrawn_total', 'updated_at'])

        withdrawal.status = Withdrawal.STATUS_PAID
        withdrawal.payment_method = payment_method
        withdrawal.transfer_no = batch.external_transfer_no
        withdrawal.proof_url = batch.proof_url
        withdrawal.admin_note = _append_note(withdrawal.admin_note, batch.admin_note)
        withdrawal.paid_by = _operator_or_none(operator)
        withdrawal.paid_at = paid_at
        if not withdrawal.reviewed_at:
            withdrawal.reviewed_at = paid_at
            withdrawal.reviewed_by = withdrawal.paid_by
        withdrawal.save(update_fields=[
            'status', 'payment_method', 'transfer_no', 'proof_url', 'admin_note',
            'paid_by', 'paid_at', 'reviewed_by', 'reviewed_at', 'updated_at',
        ])

        WithdrawalPayoutBatchItem.objects.create(
            batch=batch,
            withdrawal=withdrawal,
            amount_snapshot=amount,
        )

        external_text = f'，外部流水号 {batch.external_transfer_no}' if batch.external_transfer_no else ''
        write_ledger(
            wallet,
            WalletLedger.TYPE_WITHDRAWAL_PAID,
            WalletLedger.BUCKET_WITHDRAWING,
            -amount,
            reference_type='withdrawal',
            reference_id=withdrawal.withdrawal_no,
            note=f'财务已打款，付款批次 {batch.batch_no}{external_text}',
            operator=operator,
        )
        write_ledger(
            wallet,
            WalletLedger.TYPE_WITHDRAWAL_PAID,
            WalletLedger.BUCKET_WITHDRAWN,
            amount,
            reference_type='withdrawal',
            reference_id=withdrawal.withdrawal_no,
            note=f'提现已完成，归属付款批次 {batch.batch_no}',
            operator=operator,
        )

    return batch
