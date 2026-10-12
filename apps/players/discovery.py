"""Read-only ordering for the already filtered public discovery candidates."""
import re
from datetime import timedelta
from fractions import Fraction
from hashlib import sha256
from itertools import groupby
from zoneinfo import ZoneInfo

from django.db.models import Count, Q
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


def completion_counts(player_ids, now):
    """One grouped distinct query, with both final gates and no renewal copies.

    History needs no inferred timestamp. NULL grab_time fails only the recent
    acceptance-window predicate; it does not invalidate a verified completion.
    """
    rows = (OrderPlayer.objects.filter(player_id__in=player_ids,
            order__status=Order.STATUS_COMPLETED,
            order__order_type=Order.ORDER_TYPE_NORMAL, status='已完成')
            .values('player_id').annotate(
                recent=Count('order_id', distinct=True, filter=Q(
                    grab_time__gte=now - timedelta(days=30), grab_time__lte=now)),
                history=Count('order_id', distinct=True)))
    return {row['player_id']: {'recent': row['recent'], 'history': row['history']} for row in rows}


def presence_online(player, now):
    """Same legacy presence window, evaluated at the request's one clock."""
    if getattr(player, 'is_archived', False) or player.presence_seen_at is None:
        return False
    return timedelta(0) <= now - player.presence_seen_at < timedelta(seconds=900)


def quality_score(player, counts, now):
    """60/20/10/10, fixed 10/20 order caps, and exact partial profile points."""
    profile = getattr(player.user, 'client_profile', None) if player.user else None
    fields = (profile.avatar_url if profile else '', player.bio, player.audio_intro_url)
    profile_items = sum(bool((value or '').strip()) for value in fields)
    return (6 * min(counts.get('recent', 0), 10) + min(counts.get('history', 0), 20)
            + 10 * presence_online(player, now) + Fraction(10 * profile_items, 3))


def order_players(queryset, mode, now, shuffle_key=''):
    """Both modes prioritize acceptance willingness; never change eligibility."""
    players = list(queryset)
    day = now.astimezone(ZoneInfo('Asia/Shanghai')).date()
    if mode == 'random':
        seed = shuffle_key or 'default'
        return sorted(players, key=lambda player: (
            not (player.is_online and player.can_accept_orders),
            sha256(f'{day.isoformat()}:{seed}:{player.pk}'.encode('ascii')).digest(), player.pk))
    counts = completion_counts([player.pk for player in players], now)

    def quality_key(player):
        return (not (player.is_online and player.can_accept_orders),
                -quality_score(player, counts.get(player.pk, {}), now))

    players.sort(key=lambda player: (*quality_key(player), player.pk))
    result = []
    for _, tied_players in groupby(players, key=quality_key):
        tied = list(tied_players)
        # Rotate only within this willingness/score tie, once per Beijing day.
        offset = day.toordinal() % len(tied)
        result.extend(tied[offset:] + tied[:offset])
    return result
