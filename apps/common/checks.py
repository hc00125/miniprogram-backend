from django.conf import settings
from django.core.checks import Error, Warning, Tags, register


@register(Tags.security)
def production_security_settings_check(app_configs, **kwargs):
    messages = []
    if settings.DEBUG:
        return messages

    if '*' in settings.ALLOWED_HOSTS or not settings.ALLOWED_HOSTS:
        messages.append(Error(
            '生产环境 DJANGO_ALLOWED_HOSTS 不能为空或使用 *',
            id='miniprogram.E001',
        ))

    if getattr(settings, 'CORS_ALLOW_ALL_ORIGINS', False):
        messages.append(Error(
            '生产环境不能开启 CORS_ALLOW_ALL_ORIGINS',
            id='miniprogram.E002',
        ))

    if settings.ENABLE_MOCK_PAYMENT:
        messages.append(Error(
            '生产环境必须关闭 ENABLE_MOCK_PAYMENT',
            id='miniprogram.E003',
        ))

    if settings.ENABLE_DEV_OPENID_LOGIN:
        messages.append(Error(
            '生产环境必须关闭 ENABLE_DEV_OPENID_LOGIN',
            id='miniprogram.E004',
        ))

    required_settings = {
        'WECHAT_APP_ID': settings.WECHAT_APP_ID,
        'WECHAT_APP_SECRET': settings.WECHAT_APP_SECRET,
        'WECHATPAY_MCH_ID': settings.WECHATPAY_MCH_ID,
        'WECHATPAY_MERCHANT_SERIAL_NO': settings.WECHATPAY_MERCHANT_SERIAL_NO,
        'WECHATPAY_MERCHANT_PRIVATE_KEY_PATH': settings.WECHATPAY_MERCHANT_PRIVATE_KEY_PATH,
        'WECHATPAY_API_V3_KEY': settings.WECHATPAY_API_V3_KEY,
        'WECHATPAY_PUBLIC_KEY_ID': settings.WECHATPAY_PUBLIC_KEY_ID,
        'WECHATPAY_PUBLIC_KEY_PATH': settings.WECHATPAY_PUBLIC_KEY_PATH,
        'WECHATPAY_NOTIFY_URL': settings.WECHATPAY_NOTIFY_URL,
    }
    missing = [name for name, value in required_settings.items() if not value]
    if missing:
        messages.append(Error(
            f'生产环境缺少必要配置: {", ".join(missing)}',
            id='miniprogram.E005',
        ))

    if settings.SECRET_KEY == 'dev-secret-key-change-in-production':
        messages.append(Error(
            '生产环境必须设置 DJANGO_SECRET_KEY',
            id='miniprogram.E006',
        ))

    if settings.DATABASES['default']['ENGINE'] == 'django.db.backends.sqlite3':
        messages.append(Warning(
            '生产环境仍在使用 SQLite，支付和并发抢单场景建议迁移到 PostgreSQL 或 MySQL',
            id='miniprogram.W001',
        ))

    return messages
