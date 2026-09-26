from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from .models import EarningsConfig, PlayerWallet, WalletLedger


CENT = Decimal('0.01')
ZERO = Decimal('0.00')


def qmoney(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def get_earnings_config():
    config, _created = EarningsConfig.objects.get_or_create(
        key='default',
        defaults={
            'default_commission_rate': Decimal('16.00'),
            'review_days': 3,
            'min_withdrawal_amount': Decimal('10.00'),
        },
    )
    return config


def get_or_lock_wallet(player):
    wallet, _created = PlayerWallet.objects.get_or_create(player=player)
    return PlayerWallet.objects.select_for_update().get(pk=wallet.pk)


def bucket_balance(wallet, bucket):
    if bucket == WalletLedger.BUCKET_PENDING:
        return wallet.pending_balance
    if bucket == WalletLedger.BUCKET_AVAILABLE:
        return wallet.available_balance
    if bucket == WalletLedger.BUCKET_WITHDRAWING:
        return wallet.withdrawing_balance
    if bucket == WalletLedger.BUCKET_WITHDRAWN:
        return wallet.withdrawn_total
    if bucket == WalletLedger.BUCKET_DEBT:
        return wallet.debt_balance
    raise ValueError(f'Unsupported wallet bucket: {bucket}')


def write_ledger(
    wallet,
    entry_type,
    bucket,
    delta,
    reference_type='',
    reference_id='',
    note='',
    operator=None,
):
    return WalletLedger.objects.create(
        wallet=wallet,
        entry_type=entry_type,
        bucket=bucket,
        delta=qmoney(delta),
        balance_after=qmoney(bucket_balance(wallet, bucket)),
        reference_type=reference_type,
        reference_id=str(reference_id or ''),
        note=note or '',
        operator=operator if getattr(operator, 'is_authenticated', False) else None,
    )


@transaction.atomic
def ensure_wallet(player):
    return get_or_lock_wallet(player)
