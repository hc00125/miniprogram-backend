"""Compatibility import for v2's fixed policy, never personal gift settings.

Historical PlayerGiftConfig rows stay intact. Ordinary gifts still use
apps.gifts.services.configuration.resolve_commission. No wages are rewritten.
"""
from django.core.exceptions import ValidationError
from apps.orders.surcharges import SURCHARGE_POLICY_VERSION


def get_surcharge_commission(player=None, *, expected_version=None):
    if expected_version is not None and expected_version != SURCHARGE_POLICY_VERSION:
        raise ValidationError('CONFIG_CHANGED：加价已改为固定25%政策，请重新报价')
    return {'commission_rate': '25.00', 'version': SURCHARGE_POLICY_VERSION,
            'policy_version': SURCHARGE_POLICY_VERSION, 'is_enabled': True}
