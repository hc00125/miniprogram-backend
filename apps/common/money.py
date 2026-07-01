from decimal import Decimal, ROUND_HALF_UP


CENT = Decimal('0.01')


def money(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def money_float(value):
    return float(money(value))
