"""Naming service promises; display validity and day-pass pricing stay separate."""
from decimal import Decimal, ROUND_HALF_UP

# Exact public PlayerType names verified through /api/boss/player-types.
# IDs/priority are not portable and two real levels share priority=3.
LEVEL_NAMES = ('娱乐mini', '技术mini', '娱乐pro', '技术pro', '金牌陪', '明星陪')
DEFAULT_HOURS = {
    'day': ('5', '4', '3', '2.5', '2', '1.5'),
    'week': ('14', '10', '8', '6', '5', '4'),
    'month': ('33', '25', '20', '16', '13', '10'),
    'quarter': ('78', '58', '48', '36', '29', '23'),
    'year': ('268', '208', '168', '128', '105', '82'),
}
TERM_FIELDS = ('service_hours', 'hourly_amount_yuan', 'hourly_amount_diamonds', 'service_terms_source', 'service_terms_version')


def duration_overrides(player):
    from .models import PlayerNamingServiceConfig
    return dict(PlayerNamingServiceConfig.objects.filter(player=player).values_list('package_code', 'service_hours')) if player else {}


def service_terms(player, code, price, overrides=None):
    empty = dict.fromkeys(TERM_FIELDS)
    if not player or code not in DEFAULT_HOURS:
        return empty
    overrides = duration_overrides(player) if overrides is None else overrides
    name = player.player_type.name
    if code in overrides:
        hours = overrides[code]
        source = 'player_override'
    elif name in LEVEL_NAMES:
        hours = Decimal(DEFAULT_HOURS[code][LEVEL_NAMES.index(name)])
        source = 'level_default'
    else:
        return empty
    return dict(service_hours=format(hours.normalize(), 'f'),
                hourly_amount_yuan=None if price is None else format((price / hours).quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f'),
                hourly_amount_diamonds=None if price is None else format((price * 10 / hours).quantize(Decimal('.1'), rounding=ROUND_HALF_UP), '.1f'),
                service_terms_source=source, service_terms_version='2026-10-table-v1')
