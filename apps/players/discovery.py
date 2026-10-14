"""Read-only ordering for the already filtered public discovery candidates."""
import re
from collections import defaultdict
from datetime import timedelta
from fractions import Fraction
from hashlib import sha256
from itertools import groupby
from zoneinfo import ZoneInfo

from django.db.models import Exists, OuterRef, Q
from rest_framework.exceptions import ValidationError

from apps.orders.models import Order, OrderPlayer


def discovery_params(params):
    """Do not normalize invalid mode/seed tokens or affect legacy ordering."""
    mode = params.get('discovery', '')
    seed = params.get('shuffle_key', '')
    if mode not in ('', 'quality', 'random'):
        raise ValidationError({'discovery': '仅支持 quality 或 random'})
    if mode == 'random' and seed and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', seed):
        raise ValidationError({'shuffle_key': '随机种子须为1~64位字母、数字、下划线或连字符'})
    return mode, seed


def service_metrics(player_ids, now):
    """Load every verified root completion once and derive V2 service metrics."""
    from apps.orders.models import OrderDesignation
    accepted_designation = OrderDesignation.objects.filter(
        order_id=OuterRef('order_id'),
        player_id=OuterRef('player_id'),
        status=OrderDesignation.STATUS_ACCEPTED,
    )
    rows = (OrderPlayer.objects.filter(
        player_id__in=player_ids,
        order__status=Order.STATUS_COMPLETED,
        order__order_type=Order.ORDER_TYPE_NORMAL,
        order__parent_order__isnull=True,
        status='已完成',
    ).annotate(
        accepted_designation=Exists(accepted_designation),
    ).values(
        'player_id', 'order_id', 'grab_time', 'order__boss_user_id',
        'order__target_player_id', 'accepted_designation',
    ).distinct())

    order_ids = defaultdict(set)
    recent_ids = defaultdict(set)
    boss_orders = defaultdict(lambda: defaultdict(set))
    designated_bosses = defaultdict(set)
    for row in rows:
        player_id = row['player_id']
        order_id = row['order_id']
        grabbed_at = row['grab_time']
        boss_id = row['order__boss_user_id']
        order_ids[player_id].add(order_id)
        if grabbed_at is not None and now - timedelta(days=30) <= grabbed_at <= now:
            recent_ids[player_id].add(order_id)
        if (grabbed_at is None or not now - timedelta(days=90) <= grabbed_at <= now
                or boss_id is None):
            continue
        boss_orders[player_id][boss_id].add(order_id)
        if (row['order__target_player_id'] == player_id
                or row['accepted_designation']):
            designated_bosses[player_id].add(boss_id)

    return {
        player_id: {
            'recent': len(recent_ids[player_id]),
            'history': len(ids),
            'boss_coverage': len(boss_orders[player_id]),
            'repeat_bosses': sum(len(orders) >= 2 for orders in boss_orders[player_id].values()),
            'designated_bosses': len(designated_bosses[player_id]),
        }
        for player_id, ids in order_ids.items()
    }


def completion_counts(player_ids, now):
    """Compatibility view of the verified distinct completion metrics."""
    return {
        player_id: {'recent': row['recent'], 'history': row['history']}
        for player_id, row in service_metrics(player_ids, now).items()
    }


def presence_online(player, now):
    """Same legacy presence window, evaluated at the request's one clock."""
    if getattr(player, 'is_archived', False) or player.presence_seen_at is None:
        return False
    return timedelta(0) <= now - player.presence_seen_at < timedelta(seconds=900)


def patronage_bonuses(player_ids, now):
    """Frozen V2 crown/day-pass score, highest verified entitlement only."""
    from apps.patronage.models import PatronagePurchase
    rows = (PatronagePurchase.objects.filter(
        player_id__in=player_ids, payment_status='paid',
    ).filter(
        Q(package_code='day_pass', paid_at__gte=now - timedelta(hours=24), paid_at__lte=now,
            crowns__source='day_pass_bonus', crowns__revoked_at__isnull=True,
            crowns__starts_at__lte=now, crowns__expires_at__gt=now)
        | Q(package_code__in=('day', 'week', 'month', 'quarter', 'year'),
            crowns__source='purchase', crowns__revoked_at__isnull=True,
            crowns__starts_at__lte=now, crowns__expires_at__gt=now)
    ).values_list('player_id', 'package_code').distinct())
    tier_scores = {'day': 8, 'week': 12, 'month': 16, 'quarter': 20, 'year': 25,
                   'day_pass': 20}
    bonuses = {}
    for player_id, package_code in rows:
        bonuses[player_id] = max(bonuses.get(player_id, 0), tier_scores[package_code])
    return bonuses


def activity_score(player, now):
    """Frozen V2 request-clock activity tiers; future timestamps never score."""
    if getattr(player, 'is_archived', False) or player.presence_seen_at is None:
        return 0
    age = now - player.presence_seen_at
    if age < timedelta(0):
        return 0
    if age < timedelta(hours=1):
        return 10
    if age < timedelta(hours=24):
        return 6
    if age < timedelta(days=7):
        return 2
    return 0


def stepped_score(value, table):
    score = 0
    for threshold, points in table:
        if value < threshold:
            break
        score = points
    return score


def quality_score(player, counts, now, patronage_bonus=0):
    """Frozen Commercial Recognition V2 score, exactly capped at 100."""
    profile = getattr(player.user, 'client_profile', None) if player.user else None
    fields = (profile.avatar_url if profile else '', player.bio, player.audio_intro_url)
    profile_items = sum(bool((value or '').strip()) for value in fields)
    recent = stepped_score(counts.get('recent', 0), ((1, 3), (2, 5), (3, 7),
                                                       (5, 9), (8, 10)))
    history = stepped_score(counts.get('history', 0), ((1, 1), (3, 2), (5, 3),
                                                        (10, 4), (20, 5)))
    return (
        min(patronage_bonus, 25)
        + 5 * min(counts.get('repeat_bosses', 0), 4)
        + 5 * min(counts.get('designated_bosses', 0), 3)
        + recent
        + 2 * min(counts.get('boss_coverage', 0), 5)
        + history
        + activity_score(player, now)
        + Fraction(5 * profile_items, 3)
    )


def availability_layer(player):
    """Hard business layer: idle accepters, serving accepters, then everyone else."""
    if player.is_online and player.can_accept_orders:
        return 1 if getattr(player, 'has_active_order', False) else 0
    return 2


def order_players(queryset, mode, now, shuffle_key=''):
    """Both modes use the same three hard availability layers."""
    players = list(queryset)
    day = now.astimezone(ZoneInfo('Asia/Shanghai')).date()
    if mode == 'random':
        seed = shuffle_key or 'default'
        return sorted(players, key=lambda player: (
            availability_layer(player),
            sha256(f'{day.isoformat()}:{seed}:{player.pk}'.encode('ascii')).digest(), player.pk))
    counts = service_metrics([player.pk for player in players], now)
    support = patronage_bonuses([player.pk for player in players], now)

    def quality_key(player):
        score = quality_score(player, counts.get(player.pk, {}), now, support.get(player.pk, 0))
        return availability_layer(player), -(score // 5)

    players.sort(key=lambda player: (*quality_key(player), player.pk))
    result = []
    for _, tied_players in groupby(players, key=quality_key):
        tied = list(tied_players)
        # Rotate only within this hard layer/5-point band, once per Beijing day.
        offset = day.toordinal() % len(tied)
        result.extend(tied[offset:] + tied[:offset])
    return result
