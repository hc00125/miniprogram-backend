from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import ValidationError


DIAMONDS_PER_YUAN = 10
DIAMONDS_PER_YUAN_DECIMAL = Decimal(str(DIAMONDS_PER_YUAN))
CENT = Decimal('0.01')
ZERO = Decimal('0.00')


def qyuan(value) -> Decimal:
    """Normalize a monetary value without ever passing through binary float."""
    try:
        return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError('人民币金额格式不正确') from exc


def yuan_to_diamonds(value) -> int:
    """Convert a RMB Decimal amount to an integer diamond display value.

    Backend accounting remains RMB. Diamonds are a presentation/settlement unit at
    a fixed 1 RMB = 10 diamonds ratio. Values that would produce fractional
    diamonds are rejected instead of rounded.
    """
    yuan = qyuan(value)
    diamonds = yuan * DIAMONDS_PER_YUAN_DECIMAL
    integral = diamonds.to_integral_value()
    if diamonds != integral:
        raise ValidationError('金额必须为0.1元的整数倍，不能产生小数钻石')
    return int(integral)


def diamonds_to_yuan(value) -> Decimal:
    if isinstance(value, bool):
        raise ValidationError('钻石数量必须为整数')
    try:
        diamonds = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError('钻石数量必须为整数') from exc
    if diamonds < 0 or str(value).strip() not in {str(diamonds), f'{diamonds}.0'}:
        raise ValidationError('钻石数量必须为非负整数')
    return (Decimal(diamonds) / DIAMONDS_PER_YUAN_DECIMAL).quantize(CENT)


def diamond_payload(value, *, yuan_key='amount_yuan', diamonds_key='diamonds') -> dict:
    yuan = qyuan(value)
    return {
        yuan_key: str(yuan),
        diamonds_key: yuan_to_diamonds(yuan),
    }
