import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError


# User-facing exchange ratio. This never changes: ¥1 = 10 diamonds.
DIAMONDS_PER_YUAN = 10
DIAMONDS_PER_YUAN_DECIMAL = Decimal(str(DIAMONDS_PER_YUAN))
DIAMOND_QUANTUM = Decimal('0.1')
CENT = Decimal('0.01')
ZERO = Decimal('0.00')

# Historical code used one XPay integer coin for one displayed diamond, i.e.
# 10 integer units per RMB. New coin settlement uses 100 integer units per RMB
# so one XPay unit represents ¥0.01 = 0.1 displayed diamond. The scale is
# recorded on each new recharge/payment payload so historical rows remain
# interpretable without rewriting real order data.
LEGACY_COIN_UNITS_PER_YUAN = 10
DEFAULT_COIN_UNITS_PER_YUAN = 100


def qyuan(value) -> Decimal:
    """Normalize a monetary value without ever passing through binary float."""
    try:
        return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError('人民币金额格式不正确') from exc


def yuan_to_diamonds(value) -> Decimal:
    """Convert RMB to the user-facing diamond amount at fixed 1:10.

    RMB remains the accounting source of truth to the cent. Therefore ¥12.35
    is represented exactly as 123.5 diamonds; no rounding or truncation is
    needed and historical RMB order values remain unchanged.
    """
    yuan = qyuan(value)
    return (yuan * DIAMONDS_PER_YUAN_DECIMAL).quantize(
        DIAMOND_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def format_diamonds(value) -> str:
    """Return a stable one-decimal display string, e.g. ``123.5``/``100.0``."""
    return f'{yuan_to_diamonds(value):.1f}'


def diamonds_to_yuan(value) -> Decimal:
    """Convert a user-facing diamond value (max one decimal) back to RMB."""
    if isinstance(value, bool):
        raise ValidationError('钻石数量格式不正确')
    try:
        diamonds = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError('钻石数量格式不正确') from exc
    if diamonds < 0:
        raise ValidationError('钻石数量必须为非负数')
    if diamonds != diamonds.quantize(DIAMOND_QUANTUM):
        raise ValidationError('钻石最多保留1位小数')

    yuan = diamonds / DIAMONDS_PER_YUAN_DECIMAL
    # One decimal diamond maps exactly to one RMB cent.
    return qyuan(yuan)


def coin_units_per_yuan(value=None) -> int:
    """Return the integer XPay settlement scale, independent of display ratio.

    ``config.settings`` predates this option on some deployed branches, so the
    environment is also checked directly. This keeps the setting effective
    without requiring a database/data migration and still lets Django tests use
    ``override_settings``.
    """
    if value is not None:
        raw = value
    elif hasattr(settings, 'WECHAT_VIRTUALPAY_COIN_UNITS_PER_YUAN'):
        raw = settings.WECHAT_VIRTUALPAY_COIN_UNITS_PER_YUAN
    else:
        raw = os.environ.get(
            'WECHAT_VIRTUALPAY_COIN_UNITS_PER_YUAN',
            str(DEFAULT_COIN_UNITS_PER_YUAN),
        )
    try:
        units = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError('微信代币最小单位配置不正确') from exc
    if units <= 0:
        raise ValidationError('微信代币最小单位配置必须大于0')
    return units


def yuan_to_coin_units(value, *, units_per_yuan=None) -> int:
    """Convert RMB cents to the integer unit required by XPay.

    With the new scale of 100 units/RMB, ¥12.35 becomes 1235 XPay units while
    still displaying as 123.5 diamonds. If an older 10-units/RMB scale is
    explicitly used, cent amounts that it cannot represent are rejected rather
    than silently rounded.
    """
    yuan = qyuan(value)
    scale = coin_units_per_yuan(units_per_yuan)
    units = yuan * Decimal(scale)
    integral = units.to_integral_value()
    if units != integral:
        raise ValidationError(
            f'当前微信代币精度（每元{scale}单位）无法精确表示金额{yuan:.2f}元'
        )
    return int(integral)


def coin_units_to_yuan(value, *, units_per_yuan=None) -> Decimal:
    if isinstance(value, bool):
        raise ValidationError('微信代币数量必须为整数')
    try:
        units = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError('微信代币数量必须为整数') from exc
    if units < 0 or str(value).strip() not in {str(units), f'{units}.0'}:
        raise ValidationError('微信代币数量必须为非负整数')
    scale = coin_units_per_yuan(units_per_yuan)
    raw_yuan = Decimal(units) / Decimal(scale)
    rounded = qyuan(raw_yuan)
    # Do not hide sub-cent settlement values in the RMB ledger.
    if rounded != raw_yuan:
        raise ValidationError('微信代币数量无法精确映射到人民币分')
    return rounded


def coin_units_to_diamonds(value, *, units_per_yuan=None) -> Decimal:
    return yuan_to_diamonds(
        coin_units_to_yuan(value, units_per_yuan=units_per_yuan)
    )


def diamond_payload(value, *, yuan_key='amount_yuan', diamonds_key='diamonds') -> dict:
    yuan = qyuan(value)
    return {
        yuan_key: str(yuan),
        diamonds_key: format_diamonds(yuan),
    }
