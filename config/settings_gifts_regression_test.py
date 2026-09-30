"""Full URL coverage, offline synthetic settings; never production .env/settings."""
from .settings_gifts_test import *  # noqa
ROOT_URLCONF = 'config.urls'
TEMPLATES[0]['DIRS'] = [BASE_DIR / 'templates']
KOOK_ORDER_PUBLIC_CREATED_SINCE = '2020-01-01T00:00:00+00:00'
KOOK_ENABLED = False
KOOK_SEND_ENABLED = False
KOOK_BOT_KEY = 'offline-test-bot'
KOOK_TESTING = True
KOOK_TEST_SECRETS = {'binding_pepper': 'fake-offline-pepper', 'verify_token': 'fake-verify', 'encrypt_key': '', 'bot_token': 'fake-token'}
WECHAT_APP_ID = 'offline-synthetic-app'
WECHAT_APP_SECRET = 'offline-synthetic-secret'
ENABLE_MOCK_PAYMENT = False
ENABLE_DEV_OPENID_LOGIN = False
