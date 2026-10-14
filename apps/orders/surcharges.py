"""Read-only readiness checks, not authorization to create a payment.

Public/unaccepted rules remain provisional; eligibility stays false until the
shared money engine and product decisions have been separately accepted.
"""
from django.conf import settings
from .models import Order
from .cancellation_models import OrderReplacementState
from decimal import Decimal
from math import gcd
from rest_framework.exceptions import ValidationError

SURCHARGE_POLICY_VERSION = 'surcharge-v2-fixed25-equal1'


def quote_amount(amount_diamonds, required_players):
    """Exact diamond-equivalent split; never round or distribute a remainder.

    Wallet yuan has 2dp; earnings fish crackers have 2dp. Quotation is in
    diamond-equivalent units, NOT a credit to the player's fish wallet.
    Gross/fee/net all fit 2dp iff 25 * integer diamonds is divisible by N.
    """
    if type(required_players) is not int or required_players <= 0:
        raise ValidationError({'code': 'INVALID_HEADCOUNT', 'detail': '人数必须为正整数'})
    step = required_players // gcd(25, required_players)
    if type(amount_diamonds) is not int or not 0 <= amount_diamonds <= 9999999999999:
        raise ValidationError({'code': 'INVALID_AMOUNT', 'detail': '整单加价必须为非负整数钻石'})
    if amount_diamonds % step:
        raise ValidationError({'code': 'SURCHARGE_NOT_DIVISIBLE',
            'detail': f'{required_players}人均分且固定抽成25%，整单加价须为{step}钻的整数倍；不舍入、不分配尾差',
            'amount_step_diamonds': step})
    gross = Decimal(amount_diamonds) / required_players
    fee = gross / 4
    return {'policy_version': SURCHARGE_POLICY_VERSION, 'commission_rate': '25.00',
        'amount_diamonds': amount_diamonds, 'amount_yuan': format(Decimal(amount_diamonds) / 10, '.2f'),
        'required_players': required_players, 'amount_step_diamonds': step,
        'per_person_gross_diamonds': format(gross, '.2f'),
        'per_person_commission_diamonds': format(fee, '.2f'),
        'per_person_net_diamonds': format(gross - fee, '.2f')}


def surcharge_summary(order):
    """Read-only; unknown/history/partial refunds never fabricate a reward."""
    rows = list(order.surcharges.select_related('attempt').all())
    net = Decimal('0')
    paid = pending = refunded = 0
    blockers = set()
    for row in rows:
        refunded += row.refunded_diamonds
        status = row.status
        if row.attempt_id and row.attempt.status != 'completed':
            status = {'dispatching': 'unknown', 'unknown': 'unknown',
                      'prepared': 'processing', 'succeeded': 'processing'}.get(
                          row.attempt.status, row.attempt.status)
        if status in ('created', 'processing', 'unknown'):
            pending += row.amount_diamonds
            continue
        if status == 'partially_refunded':
            blockers.add('SURCHARGE_REFUND_REQUIRES_REVIEW')
            continue
        if status != 'paid' or row.refunded_diamonds:
            continue
        try:
            expected = quote_amount(row.amount_diamonds, order.required_players)
        except ValidationError:
            blockers.add('SURCHARGE_SNAPSHOT_UNAVAILABLE')
            continue
        if row.allocation_snapshot != expected:
            blockers.add('SURCHARGE_SNAPSHOT_UNAVAILABLE')
            continue
        paid += row.amount_diamonds
        net += Decimal(expected['per_person_net_diamonds'])
    return {'policy_version': SURCHARGE_POLICY_VERSION, 'commission_rate': '25.00',
        'required_players': order.required_players, 'paid_diamonds': paid,
        'pending_diamonds': pending, 'refunded_diamonds': refunded,
        'per_person_net_diamonds': format(net, '.2f'), 'blockers': sorted(blockers),
        'earnings_withdrawable': False}


def surcharge_readiness(order):
    structural = []
    if order.order_type != Order.ORDER_TYPE_NORMAL:
        structural.append('ORDER_NOT_NORMAL')
    if order.fulfillment_mode != Order.FULFILLMENT_MODE_PUBLIC or order.target_player_id or order.designated_players:
        structural.append('ORDER_NOT_PUBLIC')
    if order.status != Order.STATUS_WAITING:
        structural.append('ORDER_NOT_WAITING')
    if order.order_players.exists():
        structural.append('ORDER_ALREADY_ACCEPTED')
    if OrderReplacementState.objects.filter(order=order).exclude(status=OrderReplacementState.STATUS_RESOLVED).exists():
        structural.append('ORDER_IN_REPLACEMENT')
    if order.surcharges.filter(status__in=('processing', 'unknown')).exists():
        structural.append('PAYMENT_PENDING')
    # Standalone appends are retired. Quotes/creation live on /boss/order.
    blockers = list(structural) + ['SURCHARGE_PREORDER_ONLY']
    if not getattr(settings, 'ORDER_SURCHARGE_ISOLATED_TEST_MODE', False):
        blockers.append('SURCHARGE_LIFECYCLE_UNAVAILABLE')
    if not getattr(settings, 'ORDER_SURCHARGE_ENABLED', False):
        blockers.append('FEATURE_DISABLED')
    p = getattr(settings, 'ORDER_SURCHARGE_POLICY', {})
    confirmed = bool(p.get('version') and p.get('platform_approved') is True and p.get('rules_approved') is True
        and all(type(p.get(k)) is int and p[k] > 0 for k in ('min_diamonds', 'max_diamonds', 'daily_diamonds')))
    if not confirmed:
        blockers.append('POLICY_UNCONFIRMED')
    from django.apps import apps
    if apps.is_installed('apps.gifts'):
        Gift = apps.get_model('gifts', 'Gift')
        if not Gift.objects.filter(code='order_surcharge', kind='internal_surcharge', internal_enabled=True).exists():
            blockers.append('SURCHARGE_CONFIG_UNAVAILABLE')
    else:
        blockers.append('SURCHARGE_CONFIG_UNAVAILABLE')
    from django.db import connection
    if connection.vendor != 'postgresql':
        blockers.append('CONCURRENCY_GUARD_UNAVAILABLE')
    return {'eligible': not blockers, 'can_submit': not blockers, 'structurally_eligible': not structural,
            'blockers': blockers, 'disabled_reason': '订单加价尚不可用，请先确认策略与并发门禁' if blockers else '',
            'min_amount_diamonds': p.get('min_diamonds') if confirmed else None,
            'max_amount_diamonds': p.get('max_diamonds') if confirmed else None}
