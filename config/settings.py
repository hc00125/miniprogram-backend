import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'dev-secret-key-change-in-production')
DEBUG = os.environ.get('DJANGO_DEBUG', 'true').lower() in {'1', 'true', 'yes', 'on'}
ALLOWED_HOSTS = [host.strip() for host in os.environ.get('DJANGO_ALLOWED_HOSTS', '*').split(',') if host.strip()]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework_simplejwt',
    'apps.common',
    'apps.accounts',
    'apps.catalog',
    'apps.players',
    'apps.orders',
    'apps.payments',
    'apps.chat',
    'apps.admin_api',
]

try:
    import corsheaders  # noqa: F401
    INSTALLED_APPS.insert(6, 'corsheaders')
except ImportError:
    pass

try:
    import drf_spectacular  # noqa: F401
    INSTALLED_APPS.append('drf_spectacular')
except ImportError:
    pass

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

if 'corsheaders' in INSTALLED_APPS:
    MIDDLEWARE.insert(0, 'corsheaders.middleware.CorsMiddleware')

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith('sqlite:///'):
    db_name = DATABASE_URL.removeprefix('sqlite:///')
else:
    db_name = os.environ.get('SQLITE_DB_PATH', str(BASE_DIR / 'db.sqlite3'))

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': db_name,
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = []
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
APPEND_SLASH = False
CORS_ALLOW_ALL_ORIGINS = DEBUG

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'apps.accounts.authentication.LegacyPlayerTokenAuthentication',
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.AllowAny',
    ),
    'EXCEPTION_HANDLER': 'apps.common.exceptions.compat_exception_handler',
}

if 'drf_spectacular' in INSTALLED_APPS:
    REST_FRAMEWORK['DEFAULT_SCHEMA_CLASS'] = 'drf_spectacular.openapi.AutoSchema'

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=int(os.environ.get('JWT_ACCESS_DAYS', '30'))),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=int(os.environ.get('JWT_REFRESH_DAYS', '90'))),
    'AUTH_HEADER_TYPES': ('Bearer',),
}

SPECTACULAR_SETTINGS = {
    'TITLE': '俱乐部点单 Django API',
    'DESCRIPTION': '兼容微信小程序现有接口契约的 Django/DRF 后端',
    'VERSION': '1.0.0',
}

WECHAT_APP_ID = os.environ.get('WECHAT_APP_ID', '')
WECHAT_APP_SECRET = os.environ.get('WECHAT_APP_SECRET', '')
ENABLE_MOCK_PAYMENT = os.environ.get('ENABLE_MOCK_PAYMENT', 'true').lower() in {'1', 'true', 'yes', 'on'}
