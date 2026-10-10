"""Offline crown-wall tests: no production settings, .env, or network access."""
import atexit
import socket
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = 'offline-crown-wall-fixture-key-not-for-production'
DEBUG = False
USE_TZ = True
TIME_ZONE = 'UTC'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
ALLOWED_HOSTS = ['testserver']
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
INSTALLED_APPS = [
    'django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes',
    'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles',
    'rest_framework', 'apps.common', 'apps.accounts', 'apps.catalog', 'apps.players',
    'apps.orders', 'apps.payments', 'apps.refunds', 'apps.earnings', 'apps.wallet',
    'apps.support', 'apps.chat', 'apps.kook_integration', 'apps.gifts',
    'apps.patronage', 'apps.dispatch',
]
# Existing deployment migrations include PostgreSQL-only DDL. Read-only API
# tests synchronize current models in SQLite; they do not validate those DDLs.
MIGRATION_MODULES = {app.rsplit('.', 1)[-1]: None for app in INSTALLED_APPS if app.startswith('apps.')}
ROOT_URLCONF = 'config.urls'
MIDDLEWARE = [
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
]
TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'APP_DIRS': True,
    'DIRS': [BASE_DIR / 'templates'],
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages',
    ]},
}]
STATIC_URL = '/static/'
MEDIA_URL = '/media/'
_media = tempfile.TemporaryDirectory(prefix='patronage-wall-tests-')
atexit.register(_media.cleanup)
MEDIA_ROOT = _media.name
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.AllowAny'],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
PATRONAGE_PURCHASE_ENABLED = False
ORDER_SURCHARGE_ISOLATED_TEST_MODE = True
FISH_CRACKER_EXCHANGE_RATE = 10
KOOK_ENABLED = KOOK_SEND_ENABLED = False
WECHAT_APP_ID = 'offline-fixture-app'
WECHAT_APP_SECRET = 'offline-fixture-secret'
ENABLE_MOCK_PAYMENT = ENABLE_DEV_OPENID_LOGIN = False


def _deny_network(*args, **kwargs):
    raise RuntimeError('Network disabled in offline crown-wall tests')


socket.socket.connect = _deny_network
socket.socket.connect_ex = _deny_network
socket.create_connection = _deny_network
