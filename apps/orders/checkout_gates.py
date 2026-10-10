"""Production admission for public pre-order surcharge checkout only.

No isolation flag and no ordinary-gift policy can grant this capability.
Defaults are closed; deployment owns the explicit acceptance flags.
"""
from django.conf import settings


def checkout_blockers():
    blockers = []
    if getattr(settings, 'ORDER_SURCHARGE_CHECKOUT_READY', False) is not True:
        blockers.append('SURCHARGE_LIFECYCLE_UNAVAILABLE')
    if getattr(settings, 'ORDER_SURCHARGE_ENABLED', False) is not True:
        blockers.append('FEATURE_DISABLED')
    if getattr(settings, 'SHARED_SPEND_PLATFORM_APPROVED', False) is not True:
        blockers.append('POLICY_UNCONFIRMED')
    return blockers
