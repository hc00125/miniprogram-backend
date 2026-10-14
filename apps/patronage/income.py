"""Internal income hook only. This module neither charges nor grants crowns.

Requires paid purchase + completed shared attempt + matching actual debit.
Rounding deliberately follows gifts/services/income.py: ROUND_HALF_UP gross
fish to cents, then commission on rounded gross, net is the exact remainder.
No independently withdrawable earning bucket; funds use existing PlayerWallet.
"""
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.earnings import wallet as earnings_wallet
from apps.earnings.models import WalletLedger
from apps.wallet.spend_service import fail
from .evidence import lock_confirmed_payment
from .models import PatronageEarning


@transaction.atomic
def credit_purchase(purchase):
    """Credit once, using database state (never caller-supplied paid flags).

    Lock client wallet -> attempt -> purchase -> player wallet, matching the
    existing shared finalize callback. Do not call while holding the purchase
    lock before the client wallet; the parent payment integration must preserve
    this order. No shared reserve/dispatch route currently admits patronage.
    """
    purchase, attempt, debit = lock_confirmed_payment(purchase.pk)
    previous = PatronageEarning.objects.filter(purchase=purchase).first()
    if previous:
        return previous
    try:
        fish_per_yuan = Decimal(str(getattr(settings, 'FISH_CRACKER_EXCHANGE_RATE', 10)))
        if (not fish_per_yuan.is_finite() or not 0 < fish_per_yuan < Decimal('1e12')
                or fish_per_yuan != fish_per_yuan.quantize(Decimal('0.000000000001'))):
            fail('INVALID_INCOME_CONFIGURATION')
    except (InvalidOperation, ValueError, TypeError):
        fail('INVALID_INCOME_CONFIGURATION')
    gross = earnings_wallet.qmoney(purchase.amount_yuan * fish_per_yuan)
    if gross >= Decimal('10000000000'):
        fail('INCOME_AMOUNT_UNSUPPORTED')
    commission = earnings_wallet.qmoney(gross * purchase.commission_rate)
    net = gross - commission
    debt_offset = credit = earnings_wallet.ZERO
    debt_ledger = credit_ledger = None
    if net:
        wallet = earnings_wallet.get_or_lock_wallet(purchase.player)
        debt_offset = min(wallet.debt_balance, net)
        credit = net - debt_offset
        wallet.debt_balance -= debt_offset
        wallet.available_balance += credit
        wallet.save(update_fields=['available_balance', 'debt_balance', 'updated_at'])
        common = {'reference_type': 'patronage_earning', 'reference_id': purchase.pk,
            'note': f'冠名/包天净收益即时入账 · {purchase.purchase_no}'}
        if debt_offset:
            debt_ledger = earnings_wallet.write_ledger(wallet, WalletLedger.TYPE_PATRONAGE_INCOME,
                WalletLedger.BUCKET_DEBT, -debt_offset, **common)
        if credit:
            credit_ledger = earnings_wallet.write_ledger(wallet, WalletLedger.TYPE_PATRONAGE_INCOME,
                WalletLedger.BUCKET_AVAILABLE, credit, **common)
    return PatronageEarning.objects.create(purchase=purchase, player=purchase.player,
        payment_ledger=debit, amount_yuan=purchase.amount_yuan,
        commission_rate=purchase.commission_rate, platform_amount_yuan=purchase.platform_amount_yuan,
        player_amount_yuan=purchase.player_amount_yuan, fish_per_yuan=fish_per_yuan,
        gross_amount=gross, commission_amount=commission, net_amount=net,
        debt_offset_amount=debt_offset, credited_amount=credit,
        debt_ledger=debt_ledger, credit_ledger=credit_ledger, release_at=timezone.now(),
        snapshot={'contract_version': '2.0', 'price_version': purchase.price_version,
            'config': purchase.config_snapshot, 'purchase_no': purchase.purchase_no,
            'package_code': purchase.package_code, 'attempt_id': attempt.pk,
            'external_id': attempt.external_id, 'payment_status': purchase.payment_status,
            'paid_at': purchase.paid_at.isoformat(), 'currency': 'fish',
            'wallet_destination': 'player_wallet', 'settlement': 'immediate',
            'rounding': 'gift_qmoney_half_up_gross_then_commission_net_remainder',
            'monetary_accrual': True})
