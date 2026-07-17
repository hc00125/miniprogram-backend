from django.apps import AppConfig


class OrdersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.orders'
    verbose_name = '订单管理'

    def ready(self):
        # 订单信号分文件加载：计价相关与付款前取消后的陪玩释放逻辑互不混杂。
        from . import cancel_signals, signals  # noqa: F401
