from django.apps import AppConfig


class OrdersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.orders'
    verbose_name = '订单管理'

    def ready(self):
        # 支付窗口使用独立的一对一审计模型，必须在应用启动时注册，供迁移、
        # 系统检查、API 和运营后台共同使用。
        from . import payment_window_models  # noqa: F401

        # 兼容既有指定邀请流程：create_designations 会在运行时读取模块级校验函数，
        # 这里替换为“基础规格 + 每位陪玩最低指定计费类型”的新规则。
        from . import designations
        from .designated_pricing import validate_designated_player_spec

        designations.validate_designated_player_spec = validate_designated_player_spec

        # 商品数量在小时制服务中表示预订时长。保持既有视图接口不变，
        # 将创建订单与购物车批量下单切换到统一的时长语义实现。
        from . import batch_views, boss_views
        from .duration_services import create_cart_orders, create_order

        boss_views.create_order_service = create_order
        batch_views.create_cart_orders = create_cart_orders

        # 订单信号分文件加载：计价、付款前取消、支付窗口固化、付款后进入房间补位互不混杂。
        from . import cancel_signals, payment_window_signals, room_entry_requeue, signals  # noqa: F401

        # 支付窗口单独作为只读运营审计页面展示。
        from . import payment_window_admin  # noqa: F401
