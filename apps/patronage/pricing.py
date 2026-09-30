"""Exact RMB prices and contract-v3 approved display durations."""
from decimal import Decimal
from django.conf import settings
from apps.wallet.diamonds import coin_units_per_yuan

PACKAGES = (
    ('day', '日冠', 1, Decimal('188.00')),
    ('week', '周冠', 7, Decimal('520.00')),
    ('month', '月冠', 30, Decimal('1314.00')),
    ('quarter', '季冠', 90, Decimal('2888.00')),
    ('year', '年冠', 365, Decimal('9999.00')),
)
PACKAGE_CHOICES = [(code, name) for code, name, _, _ in PACKAGES] + [('day_pass', '包天')]


def decimal_text(value):
    """At least two decimal places, retaining every nonzero fractional digit."""
    return format(value, f'.{max(2, -value.normalize().as_tuple().exponent)}f')


def configuration(player=None):
    from .models import PatronageSettings, PlayerPatronageConfig
    config = PatronageSettings.objects.filter(pk=1).first() or PatronageSettings()
    individual = PlayerPatronageConfig.objects.filter(player=player).first() if player else None
    return config, individual


def player_is_available(player):
    # No account auto-unban, no order-acceptance or service timer restrictions.
    if player.status != 'approved' or not player.is_publicly_visible:
        return False
    if player.user_id:
        profile = getattr(player.user, 'client_profile', None)
        if not player.user.is_active or (profile and profile.account_status != 'active'):
            return False
    return True


def catalog_data(player=None, *, config_pair=None):
    config, individual = config_pair if config_pair is not None else configuration(player)
    hourly = individual.hourly_rate_yuan if individual else None
    common_blockers = []
    if not config.enabled:
        common_blockers.append('CONFIGURATION_DISABLED')
    if not config.commission_rate.is_finite() or not 0 <= config.commission_rate <= 1:
        common_blockers.append('COMMISSION_INVALID')
    if player and not player_is_available(player):
        common_blockers.append('PLAYER_UNAVAILABLE')
    packages = []
    for code, name, days, _ in (*PACKAGES, ('day_pass', '包天', None, None)):
        price = hourly * Decimal('8') * Decimal('0.85') if code == 'day_pass' and hourly is not None else (
            None if code == 'day_pass' else getattr(config, f'{code}_price_yuan'))
        blockers = list(common_blockers)
        if code == 'day_pass':
            if price is None:
                blockers.append('HOURLY_RATE_UNSET')
            elif not hourly.is_finite() or hourly % Decimal('0.5'):
                blockers.append('HOURLY_RATE_STEP_INVALID')
                price = None
            elif not individual.enabled:
                blockers.append('CONFIGURATION_DISABLED')
        if price is not None:
            if not price.is_finite() or price <= 0:
                blockers.append('PRICE_INVALID')
                price = None
            elif price != price.quantize(Decimal('0.01')) or price * coin_units_per_yuan() != (price * coin_units_per_yuan()).to_integral_value():
                blockers.append('PRICE_PRECISION_UNSUPPORTED')
                price = None
        package = dict(code=code, kind='day_pass' if code == 'day_pass' else 'naming', name=name,
            duration_days=days, amount_yuan=None if price is None else f'{price:.2f}',
            amount_diamonds=None if price is None else f'{price * 10:.1f}', available=not blockers,
            blockers=list(dict.fromkeys(blockers)))
        if code == 'day_pass':
            package['bonus_naming_days'] = 7
        packages.append(package)
    profile = getattr(player.user, 'client_profile', None) if player and player.user_id else None
    player_data = None if player is None else dict(id=player.pk, name=player.name,
        avatar_url=profile.avatar_url if profile else '',
        hourly_rate_yuan=None if hourly is None or not hourly.is_finite() else f'{hourly:.2f}')
    return dict(contract_version='1.0', purchase_enabled=bool(config.purchase_enabled and
        getattr(settings, 'PATRONAGE_PURCHASE_ENABLED', False)), player=player_data,
        commission_rate=decimal_text(config.commission_rate) if config.commission_rate.is_finite() else None,
        packages=packages)
