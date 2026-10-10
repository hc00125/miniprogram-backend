"""Task offline fixtures: full routes, fake identity, deny external network."""
from .settings_gifts_regression_test import *  # noqa
# Moderation is an external boundary, not the subject of the payment regressions.
# Its own dedicated tests supply their own mocked responses.
WECHAT_CONTENT_SECURITY_ENABLED = False
# Synthetic defaults for old query/timeout and post-commit notification tests.
# No production configuration is imported; network remains denied.
WECHAT_VIRTUALPAY_ENV = 1
WECHAT_PLAYER_ORDER_TEMPLATE_ID = ''
WECHAT_PLAYER_ORDER_TEMPLATE_FIELDS = '{}'
TENCENT_SMS_ENABLED = False
# Explicit synthetic release acceptance; never inherited by runtime settings.
# Retired standalone protocol tests still use the base isolation flag.
ORDER_SURCHARGE_CHECKOUT_READY = True
SHARED_SPEND_PLATFORM_APPROVED = True
WECHAT_VIRTUALPAY_ENABLED = True
