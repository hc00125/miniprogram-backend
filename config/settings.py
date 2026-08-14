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

# 微信 UGC 内容安全：生产默认开启；本地开发/CI 可显式设为 false。
WECHAT_CONTENT_SECURITY_ENABLED = env_bool(
    'WECHAT_CONTENT_SECURITY_ENABLED',
    'false' if DEBUG else 'true',
)
WECHAT_CONTENT_SECURITY_HTTP_TIMEOUT = env_int('WECHAT_CONTENT_SECURITY_HTTP_TIMEOUT', 8)

# 维护模式 — 开启后所有 API 返回 503（除健康检查外）
MAINTENANCE_MODE = env_bool('MAINTENANCE_MODE', 'false')

# 功能开关 — 停用指定打手功能（加入阵容按钮变灰）
FEATURE_DESIGNATE_DISABLED = env_bool('FEATURE_DESIGNATE_DISABLED', 'false')

# 陪玩共享服务上架审核：默认人工审核；开启后仅符合等级/权限规则的普通服务自动上架。
PLAYER_SERVICE_LISTING_AUTO_APPROVE = env_bool('PLAYER_SERVICE_LISTING_AUTO_APPROVE', 'false')

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
    'apps.refunds.apps.RefundsConfig',
    'apps.earnings.apps.EarningsConfig',
    'apps.wallet.apps.WalletConfig',
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
    MIDDLEWARE.insert(2, 'corsheaders.middleware.CorsMiddleware')

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
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

DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL:
    try:
        import dj_database_url
    except ImportError as exc:
        raise RuntimeError('DATABASE_URL is set but dj-database-url is not installed') from exc
    DATABASES = {'default': dj_database_url.parse(DATABASE_URL, conn_max_age=60)}
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'apps.accounts.authentication.ClientJWTAuthentication',
        'apps.accounts.authentication.PlayerTokenAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.AllowAny'],
}

if 'drf_spectacular' in INSTALLED_APPS:
    REST_FRAMEWORK['DEFAULT_SCHEMA_CLASS'] = 'drf_spectacular.openapi.AutoSchema'

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=7),
}

CORS_ALLOW_ALL_ORIGINS = env_bool('CORS_ALLOW_ALL_ORIGINS', 'true')

WECHAT_APP_ID = os.environ.get('WECHAT_APP_ID', '')
WECHAT_APP_SECRET = os.environ.get('WECHAT_APP_SECRET', '')
ENABLE_DEV_OPENID_LOGIN = env_bool('ENABLE_DEV_OPENID_LOGIN', 'false')
ENABLE_MOCK_PAYMENT = env_bool('ENABLE_MOCK_PAYMENT', 'false')

# 微信支付（API v3）
WECHATPAY_MCH_ID = os.environ.get('WECHATPAY_MCH_ID', '')
WECHATPAY_MERCHANT_SERIAL_NO = os.environ.get('WECHATPAY_MERCHANT_SERIAL_NO', '')
WECHATPAY_MERCHANT_PRIVATE_KEY = os.environ.get('WECHATPAY_MERCHANT_PRIVATE_KEY', '')
WECHATPAY_MERCHANT_PRIVATE_KEY_PATH = os.environ.get('WECHATPAY_MERCHANT_PRIVATE_KEY_PATH', '')
WECHATPAY_API_V3_KEY = os.environ.get('WECHATPAY_API_V3_KEY', '')
WECHATPAY_PUBLIC_KEY_ID = os.environ.get('WECHATPAY_PUBLIC_KEY_ID', '')
WECHATPAY_PUBLIC_KEY = os.environ.get('WECHATPAY_PUBLIC_KEY', '')
WECHATPAY_PUBLIC_KEY_PATH = os.environ.get('WECHATPAY_PUBLIC_KEY_PATH', '')
WECHATPAY_NOTIFY_URL = os.environ.get('WECHATPAY_NOTIFY_URL', '')
WECHATPAY_DESCRIPTION_PREFIX = os.environ.get('WECHATPAY_DESCRIPTION_PREFIX', '偷吃俱乐部-')
WECHATPAY_HTTP_TIMEOUT = env_int('WECHATPAY_HTTP_TIMEOUT', 10)
WECHATPAY_TIMESTAMP_TOLERANCE_SECONDS = env_int('WECHATPAY_TIMESTAMP_TOLERANCE_SECONDS', 300)

# 微信虚拟支付
WECHAT_VIRTUALPAY_ENABLED = env_bool('WECHAT_VIRTUALPAY_ENABLED', 'false')
WECHAT_VIRTUALPAY_APP_KEY = os.environ.get('WECHAT_VIRTUALPAY_APP_KEY', '')
WECHAT_VIRTUALPAY_OFFER_ID = os.environ.get('WECHAT_VIRTUALPAY_OFFER_ID', '')
WECHAT_VIRTUALPAY_ENV = env_int('WECHAT_VIRTUALPAY_ENV', 0)
WECHAT_VIRTUALPAY_ZONE_ID = os.environ.get('WECHAT_VIRTUALPAY_ZONE_ID', '1')
WECHAT_VIRTUALPAY_CURRENCY_TYPE = os.environ.get('WECHAT_VIRTUALPAY_CURRENCY_TYPE', 'CNY')
WECHAT_VIRTUALPAY_BUY_QUANTITY = env_int('WECHAT_VIRTUALPAY_BUY_QUANTITY', 1)
WECHAT_VIRTUALPAY_PLATFORM = os.environ.get('WECHAT_VIRTUALPAY_PLATFORM', 'android')
WECHAT_VIRTUALPAY_HTTP_TIMEOUT = env_int('WECHAT_VIRTUALPAY_HTTP_TIMEOUT', 10)

# 微信订阅消息
WECHAT_PLAYER_ORDER_TEMPLATE_ID = os.environ.get('WECHAT_PLAYER_ORDER_TEMPLATE_ID', '')
WECHAT_PLAYER_ORDER_TEMPLATE_PAGE = os.environ.get('WECHAT_PLAYER_ORDER_TEMPLATE_PAGE', 'pages/player/my-orders/index')
WECHAT_PLAYER_ORDER_TEMPLATE_FIELDS = os.environ.get('WECHAT_PLAYER_ORDER_TEMPLATE_FIELDS', '')
WECHAT_SUBSCRIBE_MESSAGE_MINIPROGRAM_STATE = os.environ.get('WECHAT_SUBSCRIBE_MESSAGE_MINIPROGRAM_STATE', 'formal')

# 管理后台安全开关
ADMIN_ORDER_DELETE_ENABLED = env_bool('ADMIN_ORDER_DELETE_ENABLED', 'false')

# iOS 在线购买默认关闭；仅查询和售后能力保留。
IOS_PURCHASE_ENABLED = env_bool('IOS_PURCHASE_ENABLED', 'false')
