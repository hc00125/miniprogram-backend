"""Read existing successful consumption facts; never repair accounts or move money."""
from collections import defaultdict
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import DecimalField, Exists, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce, Greatest
from apps.payments.models import Payment, Refund
from .rankings import public_avatar

MONEY = DecimalField(max_digits=24, decimal_places=2)
ZERO = Value(Decimal('0'), output_field=MONEY)


def boss_results(start, end):
    totals = defaultdict(Decimal)
    refunds = (Refund.objects.filter(payment_id=OuterRef('pk'), status='succeeded')
               .order_by().values('payment_id').annotate(total=Sum('amount')).values('total'))
    payments = (Payment.objects.filter(status__in=['paid', 'refunded'], paid_at__gte=start,
                                       paid_at__lt=end, order__boss_user__isnull=False)
                .annotate(net=Greatest(ZERO, F('amount') - Coalesce(Subquery(refunds), ZERO)))
                .order_by().values('order__boss_user_id').annotate(total=Sum('net')))
    for row in payments:
        totals[row['order__boss_user_id']] += row['total'] * 10
    from apps.patronage.models import PatronagePurchase
    from apps.wallet.models import ClientWalletLedger
    debit = ClientWalletLedger.objects.filter(
        wallet_id=OuterRef('attempt__wallet_id'), entry_type='shared_spend',
        reference_type='patronage', reference_id=OuterRef('attempt__external_id'),
        amount=-OuterRef('amount_yuan'))
    patronages = (PatronagePurchase.objects.filter(
        payment_status='paid', paid_at__gte=start, paid_at__lt=end,
        attempt__status='completed', attempt__kind='patronage',
        attempt__business_no=F('purchase_no'), attempt__idempotency_key=F('idempotency_key'),
        attempt__wallet__profile__user_id=F('boss_id'), attempt__amount=F('amount_yuan'),
    ).filter(Exists(debit)).order_by().values('boss_id').annotate(total=Sum('amount_yuan')))
    for row in patronages:
        totals[row['boss_id']] += row['total'] * 10
    from apps.gifts.models import GiftPurchase, GiftRefund
    gift_debit = ClientWalletLedger.objects.filter(
        wallet_id=OuterRef('attempt__wallet_id'), entry_type='shared_spend',
        reference_type='gift', reference_id=OuterRef('attempt__external_id'),
        amount=-OuterRef('attempt__amount')).order_by('pk')
    gift_refunds = (GiftRefund.objects.filter(purchase_id=OuterRef('pk'), status='completed')
                    .order_by().values('purchase_id').annotate(total=Sum('diamonds')).values('total'))
    gifts = (GiftPurchase.objects.filter(status__in=['paid', 'partially_refunded', 'refunded'],
        gift__kind='gift', attempt__status='completed', attempt__kind='gift',
        attempt__business_no=F('purchase_no'), attempt__wallet__profile__user_id=F('buyer_id'))
        .annotate(debited_at=Subquery(gift_debit.values('created_at')[:1]),
                  net=Greatest(ZERO, F('attempt__amount') * 10 - Coalesce(
                      Subquery(gift_refunds, output_field=MONEY), ZERO)))
        .filter(debited_at__gte=start, debited_at__lt=end)
        .order_by().values('buyer_id').annotate(total=Sum('net')))
    for row in gifts:
        totals[row['buyer_id']] += row['total']
    from .surcharge_models import OrderSurcharge
    # Checkout debits include base + surcharge; base is already counted above.
    # Standalone historical surcharge debits contain only the surcharge.
    surcharge_debit = ClientWalletLedger.objects.filter(entry_type='shared_spend').filter(
        Q(reference_type='order_surcharge', wallet_id=OuterRef('attempt__wallet_id'),
          reference_id=OuterRef('attempt__external_id'), amount=-OuterRef('amount_yuan')) |
        Q(reference_type='order_checkout', wallet_id=OuterRef('ordercheckout__attempt__wallet_id'),
          reference_id=OuterRef('ordercheckout__attempt__external_id'),
          amount=-OuterRef('ordercheckout__total_amount'))).order_by('pk')
    surcharges = (OrderSurcharge.objects.filter(status__in=['paid', 'partially_refunded', 'refunded']).filter(
        Q(attempt__status='completed', attempt__kind='order_surcharge',
          attempt__wallet__profile__user_id=F('boss_id'), attempt__amount=F('amount_yuan')) |
        Q(ordercheckout__attempt__status='completed', ordercheckout__attempt__kind='order_checkout',
          ordercheckout__attempt__wallet__profile__user_id=F('boss_id')))
        .annotate(debited_at=Subquery(surcharge_debit.values('created_at')[:1]),
                  net=Greatest(ZERO, F('amount_yuan') * 10 - F('refunded_diamonds')))
        .filter(debited_at__gte=start, debited_at__lt=end)
        .order_by().values('boss_id').annotate(total=Sum('net')))
    for row in surcharges:
        totals[row['boss_id']] += row['total']
    top = sorted(((pk, value) for pk, value in totals.items() if value > 0),
                 key=lambda item: (-item[1], item[0]))[:50]
    profiles = {row['pk']: row for row in get_user_model().objects.filter(pk__in=[pk for pk, _ in top])
                .values('pk', 'client_profile__nickname', 'client_profile__avatar_url')}
    results = []
    last_value, rank = None, 0
    for index, (pk, value) in enumerate(top, 1):
        if value != last_value:
            rank = index
        last_value = value
        profile = profiles.get(pk, {})
        results.append({'id': str(pk), 'rank': rank,
                        'name': (profile.get('client_profile__nickname') or '').strip() or '老板',
                        'avatar_url': public_avatar(profile.get('client_profile__avatar_url')),
                        'value': float(value)})
    return results
