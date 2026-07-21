import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default='false'):
    return os.environ.get(name, default).lower() in {'1', 'true', 'yes', 'on'}


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def load_local_env():
    env_path = BASE_DIR / '.env'
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding='utf-8-sig').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_local_env()

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'dev-secret-key-change-in-production')
DEBUG = env_bool('DJANGO_DEBUG', 'true')
ALLOWED_HOSTS = [host.strip() for host in os.environ.get('DJANGO_ALLOWED_HOSTS', '*').split(',') if host.strip()]

# 维护模式 — 开启后所有 API 返回 503（除健康检查外）
MAINTENANCE_MODE = env_bool('MAINTENANCE_MODE', 'false')

# 功能开关 — 停用指定打手功能（加入阵容按钮变灰）
FEATURE_DESIGNATE_DISABLED = env_bool('FEATURE_DESIGNATE_DISABLED', 'false')

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
    'apps.earnings.apps.EarningsConfig',
    'apps.support.apps.SupportConfig',
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
    'apps.common.maintenance.MaintenanceModeMiddleware',
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
    }
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

DATABASE_URL = os.environ.get('DATABASE_URL', '')

if DATABASE_URL:
    from urllib.parse import unquote, urlparse

    u = urlparse(DATABASE_URL)
    engine_map = {
        'postgres': 'django.db.backends.postgresql',
        'postgresql': 'django.db.backends.postgresql',
        'mysql': 'django.db.backends.mysql',
        'sqlite': 'django.db.backends.sqlite3',
    }
    DATABASES = {
        'default': {
            'ENGINE': engine_map.get(u.scheme, 'django.db.backends.sqlite3'),
            'NAME': u.path.lstrip('/'),
            'USER': unquote(u.username) if u.username else '',
            'PASSWORD': unquote(u.password) if u.password else '',
            'HOST': u.hostname or '',
            'PORT': str(u.port) if u.port else '',
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': os.environ.get('SQLITE_DB_PATH', str(BASE_DIR / 'db.sqlite3')),
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
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
APPEND_SLASH = False
CORS_ALLOW_ALL_ORIGINS = DEBUG

SESSION_COOKIE_SECURE = env_bool('SESSION_COOKIE_SECURE', 'false' if DEBUG else 'true')
CSRF_COOKIE_SECURE = env_bool('CSRF_COOKIE_SECURE', 'false' if DEBUG else 'true')
SECURE_SSL_REDIRECT = env_bool('SECURE_SSL_REDIRECT', 'false')
SECURE_HSTS_SECONDS = env_int('SECURE_HSTS_SECONDS', '0' if DEBUG else '31536000')
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool('SECURE_HSTS_INCLUDE_SUBDOMAINS', 'false' if DEBUG else 'true')
SECURE_HSTS_PRELOAD = env_bool('SECURE_HSTS_PRELOAD', 'false')
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https') if env_bool('SECURE_PROXY_SSL_HEADER', 'true') else None

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'apps.accounts.authentication.LegacyPlayerTokenAuthentication',
        'apps.accounts.authentication.LenientJWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.AllowAny',
    ),
    'DEFAULT_PAGINATION_CLASS': 'apps.common.pagination.OptionalPageNumberPagination',
    'PAGE_SIZE': env_int('DRF_PAGE_SIZE', '20'),
    # 'DEFAULT_THROTTLE_CLASSES': (
    #     'rest_framework.throttling.AnonRateThrottle',
    #     'rest_framework.throttling.UserRateThrottle',
    # ),
    # 'DEFAULT_THROTTLE_RATES': {
    #     'anon': os.environ.get('DRF_THROTTLE_ANON', '300/min'),
    #     'user': os.environ.get('DRF_THROTTLE_USER', '600/min'),
    # },
    'COERCE_DECIMAL_TO_STRING': False,
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
ENABLE_DEV_OPENID_LOGIN = env_bool('ENABLE_DEV_OPENID_LOGIN', 'false')

# Local development can keep mock payment enabled. Production must explicitly set it to false.
ENABLE_MOCK_PAYMENT = env_bool('ENABLE_MOCK_PAYMENT', 'true')

# WeChat Pay API v3 (ordinary merchant + Mini Program/JSAPI payment).
WECHATPAY_MCH_ID = os.environ.get('WECHATPAY_MCH_ID', '')
WECHATPAY_MERCHANT_SERIAL_NO = os.environ.get('WECHATPAY_MERCHANT_SERIAL_NO', '')
WECHATPAY_MERCHANT_PRIVATE_KEY_PATH = os.environ.get('WECHATPAY_MERCHANT_PRIVATE_KEY_PATH', '')
WECHATPAY_API_V3_KEY = os.environ.get('WECHATPAY_API_V3_KEY', '')
WECHATPAY_PUBLIC_KEY_ID = os.environ.get('WECHATPAY_PUBLIC_KEY_ID', '')
WECHATPAY_PUBLIC_KEY_PATH = os.environ.get('WECHATPAY_PUBLIC_KEY_PATH', '')
WECHATPAY_NOTIFY_URL = os.environ.get('WECHATPAY_NOTIFY_URL', '')
WECHATPAY_DESCRIPTION_PREFIX = os.environ.get('WECHATPAY_DESCRIPTION_PREFIX', '偷吃俱乐部-')
WECHATPAY_HTTP_TIMEOUT = float(os.environ.get('WECHATPAY_HTTP_TIMEOUT', '10'))
WECHATPAY_TIMESTAMP_TOLERANCE_SECONDS = int(os.environ.get('WECHATPAY_TIMESTAMP_TOLERANCE_SECONDS', '300'))

# WeChat Mini Program Virtual Payment / XPay.
# env: 1=sandbox, 0=production. AppKey must only exist on the backend.
WECHAT_VIRTUALPAY_ENABLED = env_bool('WECHAT_VIRTUALPAY_ENABLED', 'false')
WECHAT_VIRTUALPAY_ENV = env_int('WECHAT_VIRTUALPAY_ENV', '1')
WECHAT_VIRTUALPAY_OFFER_ID = os.environ.get('WECHAT_VIRTUALPAY_OFFER_ID', '')
WECHAT_VIRTUALPAY_APP_KEY = os.environ.get('WECHAT_VIRTUALPAY_APP_KEY', '')
WECHAT_VIRTUALPAY_HTTP_TIMEOUT = float(os.environ.get('WECHAT_VIRTUALPAY_HTTP_TIMEOUT', '10'))

# Sandbox-only fallback for the first fixed-price test item. A database binding takes priority.
WECHAT_VIRTUALPAY_SANDBOX_PRODUCT_ID = os.environ.get('WECHAT_VIRTUALPAY_SANDBOX_PRODUCT_ID', 'escort_15')
WECHAT_VIRTUALPAY_SANDBOX_PRICE_FEN = env_int('WECHAT_VIRTUALPAY_SANDBOX_PRICE_FEN', '1500')
WECHAT_VIRTUALPAY_SANDBOX_PACKAGE_KEYWORD = os.environ.get('WECHAT_VIRTUALPAY_SANDBOX_PACKAGE_KEYWORD', '四套四弹')

# 人民币 → 鱼干兑换比例：每 1 元 RMB = ? 鱼干
FISH_CRACKER_EXCHANGE_RATE = env_int('FISH_CRACKER_EXCHANGE_RATE', '10')

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'loggers': {
        'apps.payments.virtualpay': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}
