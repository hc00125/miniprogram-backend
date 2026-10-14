"""Offline release admission tests: production gate, not isolation bypass.

Never imports runtime settings or .env. Inherited socket deny is mandatory.
"""
from .settings_surcharge_v2_test import *  # noqa
ORDER_SURCHARGE_ISOLATED_TEST_MODE = False
ORDER_SURCHARGE_ENABLED = True
ORDER_SURCHARGE_CHECKOUT_READY = True
SHARED_SPEND_PLATFORM_APPROVED = True
GIFT_PURCHASE_ENABLED = False
GIFT_TRANSACTION_POLICY = {}
