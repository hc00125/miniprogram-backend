from .settings_dispatch_pg_test import *  # noqa
# Match production authenticator classes; default isolated finance tests intentionally disable them.
REST_FRAMEWORK = dict(REST_FRAMEWORK, DEFAULT_AUTHENTICATION_CLASSES=(
    'apps.accounts.authentication.LegacyPlayerTokenAuthentication',
    'apps.accounts.authentication.LenientJWTAuthentication',
))
