from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import ValidationError


# User-facing exchange ratio. This never changes: ¥1 = 10 diamonds.
DIAMONDS_PER_YUAN = 10
DIAMONDS_PER_YUAN_DECIMAL = Decimal(str(DIAMONDS_PER_YUAN))
DIAMOND_QUANTUM = Decimal('0.1')
CENT = Decimal('0.01')
ZERO = Decimal('0.00')

# The Mini Program coin configuration has already been published in WeChat as
# 1 RMB = 10 official coin units. Published coin ratios cannot be treated as a
# runtime tuning knob: new XPay recharge/spend requests must therefore always
# use 10 units/RMB. Explicit historical scales are still accepted when reading
# old rows so existing audit data remains interpretable.
PUBLISHED_COIN_UNITS_PER_YUAN = 10
LEGACY_COIN_UNITS_PER_YUAN = 10
DEFAULT_COIN_UNITS_PER_YUAN = PUBLISHED_COIN_UNITS_PER_YUAN


def qyuan(value) -> Decimal:
    """Normalize a monetary value without ever passing through binary float."""
    try:
        return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError('人民币金额格式不正确') from exc


def yuan_to_diamonds(value) -> Decimal:
    """Convert RMB to the user-facing diamond amount at fixed 1:10.

    RMB remains the accounting source of truth to the cent. Therefore historical
    or local ledger values such as ¥12.35 can still be displayed as 123.5
    diamonds without rewriting existing order data.
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
    return qyuan(yuan)


def coin_units_per_yuan(value=None) -> int:
    """Return the integer XPay settlement scale.

    New requests are hard-locked to the already-published WeChat ratio 1:10 so
    a stale server environment variable can never accidentally make XPay charge
    ten times the intended quantity. ``value`` is only for interpreting a scale
    explicitly recorded on a historical row.
    """
    raw = PUBLISHED_COIN_UNITS_PER_YUAN if value is None else value
    try:
        units = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError('微信代币最小单位配置不正确') from exc
    if units <= 0:
        raise ValidationError('微信代币最小单位配置必须大于0')
    return units


def yuan_to_coin_units(value, *, units_per_yuan=None) -> int:
    """Convert RMB to the integer unit required by XPay without rounding."""
    yuan = qyuan(value)
    scale = coin_units_per_yuan(units_per_yuan)
    units = yuan * Decimal(scale)
    integral = units.to_integral_value()
    if units != integral:
        if scale == PUBLISHED_COIN_UNITS_PER_YUAN:
            raise ValidationError(
                f'微信代币已发布为1元=10代币，金额{yuan:.2f}元无法精确结算；'
                '微信充值和官方代币扣款金额必须为0.10元的整数倍'
            )
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
