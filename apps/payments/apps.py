from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.payments'
    verbose_name = '支付管理'

    def ready(self):
        # 微信支付单的有效期不得晚于业务订单的阵容保留截止时间。
        from . import payment_deadline_signals  # noqa: F401
