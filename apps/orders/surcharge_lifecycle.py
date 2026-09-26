"""Surcharge income: independent monetary source, original review lifecycle.

Callers lock the parent before lineup/completion. No production activation here.
Legacy paid rows without the settlement snapshot remain manual-review only.
"""
from decimal import Decimal, InvalidOperation
from django.conf import settings
from rest_framework.exceptions import ValidationError
from .surcharges import quote_amount

LIFECYCLE = 'original-order-v1'


from contextlib import contextmanager
from django.db import connection


@contextmanager
def lineup_write(order):
    """Narrow service admission; trigger still validates the paid snapshot.

    Parent is locked by the caller. No child->parent lock in triggers and no
    admission at all for ordinary orders. Restore even inside caller atomic.
    """
    if not order.surcharge_guarded:
        yield
        return
    paid_sources(order)
    if not connection.in_atomic_block:
        raise RuntimeError('Surcharge lineup requires parent transaction')
    previous = ''
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('touchi.surcharge_lineup', true)")
            previous = cursor.fetchone()[0] or ''
            cursor.execute("SELECT set_config('touchi.surcharge_lineup', %s, true)", [str(order.pk)])
    try:
        yield
    finally:
        if connection.vendor == 'postgresql' and not connection.needs_rollback:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('touchi.surcharge_lineup', %s, true)", [previous])


def settlement_snapshot(quote):
    rate = Decimal(str(settings.FISH_CRACKER_EXCHANGE_RATE))
    if not rate.is_finite() or rate <= 0:
        raise ValidationError({'code': 'SURCHARGE_EXCHANGE_INVALID'})
    gross = Decimal(quote['amount_yuan']) * rate / quote['required_players']
    fee = gross / 4
    net = gross - fee
    if any(v != v.quantize(Decimal('.01')) for v in (gross, fee, net)):
        raise ValidationError({'code': 'SURCHARGE_NOT_DIVISIBLE', 'detail': '加价鱼干不能精确均分，请调整整单加价'})
    if any(v > Decimal('9999999999.99') for v in (gross, fee, net)):
        raise ValidationError({'code': 'INVALID_AMOUNT'})
    return {'version': quote['policy_version'], 'lifecycle': LIFECYCLE,
        'fish_per_yuan': str(rate), 'per_person_gross_fish': format(gross, '.2f'),
        'per_person_commission_fish': format(fee, '.2f'), 'per_person_net_fish': format(net, '.2f')}


def paid_sources(order):
    rows = list(order.surcharges.select_related('attempt').order_by('id'))
    valid = []
    for row in rows:
        if row.status in ('failed', 'cancelled', 'refunded'):
            continue
        if row.status in ('created', 'processing', 'unknown'):
            raise ValidationError({'code': 'SURCHARGE_PAYMENT_PENDING', 'detail': '加价付款待确认'})
        snapshot = row.policy_snapshot
        if (row.status != 'paid' or row.refunded_diamonds or not row.attempt_id
                or row.attempt.status != 'completed' or not order.paid
                or row.allocation_snapshot != quote_amount(row.amount_diamonds, order.required_players)
                or snapshot.get('lifecycle') != LIFECYCLE):
            raise ValidationError({'code': 'SURCHARGE_ALLOCATION_UNCONFIRMED',
                'detail': '加价支付或历史分配快照不完整，请人工核验'})
        try:
            gross = Decimal(snapshot['per_person_gross_fish'])
            fee = Decimal(snapshot['per_person_commission_fish'])
            net = Decimal(snapshot['per_person_net_fish'])
            rate = Decimal(snapshot['fish_per_yuan'])
            assert all(v.is_finite() and v >= 0 and v == v.quantize(Decimal('.01')) for v in (gross, fee, net))
            assert rate > 0 and gross * order.required_players == row.amount_yuan * rate
            assert fee * 4 == gross and net + fee == gross
        except (KeyError, InvalidOperation, AssertionError, TypeError, ValueError):
            raise ValidationError({'code': 'SURCHARGE_SNAPSHOT_UNAVAILABLE'})
        valid.append(row)
    return valid


def create_surcharge_earnings(order, base_earnings):
    """Same parent transaction and exact review deadline as original wages."""
    from apps.earnings.models import PlayerEarning, WalletLedger
    from apps.earnings.wallet import get_or_lock_wallet, qmoney, write_ledger
    rows = paid_sources(order)
    if not rows:
        return []
    members = list(order.order_players.order_by('id'))
    if len(members) != order.required_players or any(m.status != '已完成' for m in members):
        raise ValidationError({'code': 'SURCHARGE_LINEUP_REQUIRES_REVIEW'})
    base_by_player = {e.player_id: e for e in base_earnings}
    result = []
    for row in rows:
        existing = {e.player_id: e for e in row.earnings.all()}
        if existing and set(existing) != {m.player_id for m in members}:
            raise ValidationError({'code': 'SURCHARGE_LINEUP_REQUIRES_REVIEW'})
        for slot, member in enumerate(members, 1):
            if member.player_id in existing:
                result.append(existing[member.player_id])
                continue
            snap = row.policy_snapshot
            earning = PlayerEarning.objects.create(order=order, player_id=member.player_id,
                source=PlayerEarning.SOURCE_SURCHARGE, surcharge=row,
                gross_amount=Decimal(snap['per_person_gross_fish']), commission_rate=Decimal('25'),
                commission_amount=Decimal(snap['per_person_commission_fish']),
                net_amount=Decimal(snap['per_person_net_fish']), status=PlayerEarning.STATUS_PENDING,
                review_until=base_by_player[member.player_id].review_until,
                source_snapshot={'settlement': snap, 'allocation': row.allocation_snapshot,
                    'attempt_id': row.attempt_id, 'external_id': row.attempt.external_id,
                    'slot': slot, 'order_player_id': member.pk,
                    'member_ids': [m.player_id for m in members],
                    'cancellation_ids': list(order.player_cancellations.order_by('pk').values_list('pk', flat=True))})
            wallet = get_or_lock_wallet(member.player)
            wallet.pending_balance = qmoney(wallet.pending_balance + earning.net_amount)
            wallet.save(update_fields=['pending_balance', 'updated_at'])
            write_ledger(wallet, WalletLedger.TYPE_EARNING_CREATED, WalletLedger.BUCKET_PENDING,
                earning.net_amount, reference_type='earning', reference_id=earning.pk,
                note=f'订单 {order.order_no} 独立加价工资，与原单同审核期限')
            result.append(earning)
    return result
