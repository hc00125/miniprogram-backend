"""Approved ordinary-service commission. No gift/patronage/surcharge callers."""
from decimal import Decimal

POLICY = 'standard25-mini16-v1'
MINI_NAMES = frozenset(('娱乐mini', '技术mini'))


def ordinary_rate(order, player):
    package = order.package
    ordinary = package.product_type == 'normal' and not package.requires_escort_qualification
    return Decimal('16.00') if ordinary and player.player_type.name in MINI_NAMES else Decimal('25.00')
