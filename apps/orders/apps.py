from django.apps import AppConfig


class OrdersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.orders'
    verbose_name = '订单管理'

    def ready(self):
        # 兼容既有指定邀请流程：create_designations 会在运行时读取模块级校验函数，
        # 这里替换为“基础规格 + 每位陪玩最低指定计费类型”的新规则。
        from . import designations
        from .designated_pricing import validate_designated_player_spec

        designations.validate_designated_player_spec = validate_designated_player_spec

        # 订单信号分文件加载：计价相关与付款前取消后的陪玩释放逻辑互不混杂。
        from . import cancel_signals, signals  # noqa: F401
