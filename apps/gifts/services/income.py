"""New gift deliveries credit the existing fish wallet, not a second wallet.

Purchase snapshots are in diamonds (10 diamonds per equivalent yuan). Fish
conversion uses the existing earnings exchange rate. No order or withdrawal is
fabricated; existing non-order wallet withdrawals already support this credit.
"""
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from apps.earnings import wallet as earnings_wallet
from apps.earnings.models import WalletLedger
from apps.gifts.models import GiftEarning, GiftTransfer
from apps.wallet import spend_service as spend


@transaction.atomic
def credit_entitlement(transfer, *, allocation=None, eligible=True, source=None):
    # Serialize per delivery, including separate inventory allocations. Returning
    # an existing record never backfills historical frozen/zero entitlements.
    transfer = GiftTransfer.objects.select_for_update().get(pk=transfer.pk)
    previous = GiftEarning.objects.filter(transfer=transfer, allocation=allocation).first()
    if previous:
        return previous
    source = source or {}
    quantity = allocation.quantity if allocation else transfer.quantity
    rate = Decimal(str(transfer.commission_snapshot['commission_rate']))
    fish_per_yuan = Decimal(str(getattr(settings, 'FISH_CRACKER_EXCHANGE_RATE', 10)))
    if not rate.is_finite() or not 0 <= rate <= 100 or not fish_per_yuan.is_finite() or fish_per_yuan <= 0:
        spend.fail('INVALID_INCOME_CONFIGURATION')
    gross = earnings_wallet.ZERO
    if eligible:
        unit = source.get('unit_diamonds')
        if type(unit) is not int or unit <= 0:
            spend.fail('INVALID_GIFT_COST_SNAPSHOT')
        gross = earnings_wallet.qmoney(Decimal(unit) * quantity / 10 * fish_per_yuan)
    # Round the transfer's cumulative commission, not each lot independently.
    # Thus splitting one gift into lots cannot create extra fractional fish.
    totals = GiftEarning.objects.filter(transfer=transfer).aggregate(
        gross=Sum('gross_amount'), commission=Sum('commission_amount'))
    previous_gross = earnings_wallet.qmoney(totals['gross'])
    previous_commission = earnings_wallet.qmoney(totals['commission'])
    commission = earnings_wallet.qmoney(earnings_wallet.qmoney((previous_gross + gross) * rate / 100) - previous_commission)
    net = earnings_wallet.qmoney(gross - commission)
    earning = GiftEarning.objects.create(
        transfer=transfer, allocation=allocation, player=transfer.recipient,
        earnings_eligible=eligible, status='available', release_at=timezone.now(),
        gross_amount=gross, commission_amount=commission, net_amount=net,
        # No separately withdrawable GiftEarning bucket: the actual funds live
        # in PlayerWallet. Keep historical metadata distinct from live balance.
        available_amount=earnings_wallet.ZERO,
        snapshot={'commission': transfer.commission_snapshot, 'source': source,
            'quantity': quantity, 'blockers': [], 'monetary_accrual': True,
            'currency': 'fish', 'wallet_destination': 'player_wallet',
            'fish_per_yuan': str(fish_per_yuan), 'settlement': 'immediate',
            'credited_amount': '0.00'},
    )
    if net:
        wallet = earnings_wallet.get_or_lock_wallet(transfer.recipient)
        debt_offset = min(earnings_wallet.qmoney(wallet.debt_balance), net)
        credit = earnings_wallet.qmoney(net - debt_offset)
        wallet.debt_balance = earnings_wallet.qmoney(wallet.debt_balance - debt_offset)
        wallet.available_balance = earnings_wallet.qmoney(wallet.available_balance + credit)
        wallet.save(update_fields=['available_balance', 'debt_balance', 'updated_at'])
        common = {'reference_type': 'gift_earning', 'reference_id': earning.pk,
            'note': f'礼物净收益即时入账 · {transfer.transfer_no}'}
        ledger_ids = []
        if debt_offset:
            ledger_ids.append(earnings_wallet.write_ledger(wallet, WalletLedger.TYPE_GIFT_INCOME,
                WalletLedger.BUCKET_DEBT, -debt_offset, **common).pk)
        if credit:
            ledger_ids.append(earnings_wallet.write_ledger(wallet, WalletLedger.TYPE_GIFT_INCOME,
                WalletLedger.BUCKET_AVAILABLE, credit, **common).pk)
        earning.debt_offset_amount = debt_offset
        earning.snapshot = dict(earning.snapshot, credited_amount=str(credit), ledger_ids=ledger_ids)
        earning.save(update_fields=['debt_offset_amount', 'snapshot', 'updated_at'])
    return earning
