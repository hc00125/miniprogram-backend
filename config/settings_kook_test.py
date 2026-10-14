"""Standalone offline test settings: NEVER import production settings/.env."""
import socket
import os
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = 'offline-kook-test-only-not-a-production-secret-000000000'
DEBUG = False
USE_TZ = True
TIME_ZONE = 'UTC'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
ALLOWED_HOSTS = ['testserver']
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
# Optional disposable local PostgreSQL cluster; never accept DATABASE_URL.
_pg_dir = os.environ.get('KOOK_TEST_PG_DIR', '')
if _pg_dir:
    if not _pg_dir.startswith('/tmp/kook-A-pg.') or '/' in _pg_dir[len('/tmp/kook-A-pg.'):]:
        raise RuntimeError('Only a disposable KOOK PostgreSQL socket directory is permitted')
    DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': 'postgres', 'USER': 'postgres', 'HOST': _pg_dir, 'PORT': '55439', 'TEST': {'NAME': 'test_kook_a', 'CHARSET': 'UTF8', 'TEMPLATE': 'template0'}}}
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.admin', 'rest_framework', 'apps.common', 'apps.accounts', 'apps.catalog', 'apps.players', 'apps.orders', 'apps.payments', 'apps.refunds', 'apps.earnings', 'apps.wallet', 'apps.support', 'apps.chat', 'apps.kook_integration']
MIDDLEWARE = []
ROOT_URLCONF = 'apps.kook_integration.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'APP_DIRS': True, 'OPTIONS': {'context_processors':['django.template.context_processors.request']}}]
SILENCED_SYSTEM_CHECKS = ['admin.E402','admin.E404','admin.E408','admin.E409','admin.E410']
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
REST_FRAMEWORK = {'DEFAULT_AUTHENTICATION_CLASSES': [], 'DEFAULT_PERMISSION_CLASSES': []}
# Explicit historic fixture boundary, not a production default.
KOOK_ORDER_PUBLIC_CREATED_SINCE = '2020-01-01T00:00:00+00:00'
KOOK_ENABLED = False
KOOK_SEND_ENABLED = False
KOOK_BOT_KEY = 'offline-test-bot'
KOOK_TESTING = True
KOOK_TEST_SECRETS = {'binding_pepper': 'fake-offline-pepper', 'verify_token': 'fake-verify', 'encrypt_key': '', 'bot_token': 'fake-token'}
def _deny_network(*args, **kwargs):
    raise RuntimeError('External network disabled in KOOK isolation tests')
socket.socket.connect = _deny_network
socket.create_connection = _deny_network
