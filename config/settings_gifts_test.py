"""Offline-only settings. Never import production settings or read .env."""
import socket
import tempfile
import atexit
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = 'gift-catalog-offline-tests-not-a-production-secret'
DEBUG = False
USE_TZ = True
TIME_ZONE = 'UTC'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
ALLOWED_HOSTS = ['testserver']
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
INSTALLED_APPS = [
    'apps.dispatch',
    'django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes',
    'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles',
    'rest_framework', 'apps.gifts', 'apps.common', 'apps.accounts', 'apps.catalog',
    'apps.players', 'apps.orders', 'apps.payments', 'apps.refunds', 'apps.earnings',
    'apps.wallet', 'apps.support', 'apps.chat', 'apps.kook_integration',
]
ROOT_URLCONF = 'config.urls_gifts_test'
MIDDLEWARE = [
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
]
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'APP_DIRS': True,
              'OPTIONS': {'context_processors': [
                  'django.template.context_processors.request',
                  'django.contrib.auth.context_processors.auth',
                  'django.contrib.messages.context_processors.messages',
              ]}}]
STATIC_URL = '/static/'
MEDIA_URL = '/media/'
_scratch = Path('/root/.hermes/cache/scratch')
_scratch.mkdir(parents=True, exist_ok=True)
_media = tempfile.TemporaryDirectory(prefix='gift-catalog-media-', dir=_scratch)
atexit.register(_media.cleanup)
MEDIA_ROOT = _media.name
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
REST_FRAMEWORK = {'DEFAULT_AUTHENTICATION_CLASSES': [],
                  'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.AllowAny']}
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
GIFT_CATALOG_ENABLED = True
GIFT_PURCHASE_ENABLED = False
GIFT_INVENTORY_SEND_ENABLED = False
GIFT_EARNINGS_RELEASE_ENABLED = False
GIFT_REFUND_ENABLED = False
GIFT_ADMIN_FREE_GRANT_ENABLED = False
ORDER_SURCHARGE_ENABLED = False
ORDER_SURCHARGE_ISOLATED_TEST_MODE = True  # Offline test DB + network deny only
# Existing earnings test fixture, not a gift/surcharge settlement policy.
FISH_CRACKER_EXCHANGE_RATE = 10

def _deny_network(*args, **kwargs):
    raise RuntimeError('Network disabled in gift catalog isolation tests')

socket.socket.connect = _deny_network
socket.socket.connect_ex = _deny_network
socket.create_connection = _deny_network
