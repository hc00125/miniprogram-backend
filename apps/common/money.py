from decimal import Decimal, ROUND_HALF_UP


def money(value):
    return float(Decimal(str(value or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
