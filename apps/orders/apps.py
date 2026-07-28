from django.apps import AppConfig


class OrdersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.orders'
    verbose_name = '订单管理'

    def ready(self):
        # 独立模型必须在应用启动时注册，供迁移、API 和运营后台共同使用。
        from . import cancellation_models, payment_window_models  # noqa: F401

        # 兼容既有指定邀请流程：create_designations 会在运行时读取模块级校验函数。
        from . import designations
        from .designated_pricing import validate_designated_player_spec

        designations.validate_designated_player_spec = validate_designated_player_spec

        # 商品数量在小时制服务中表示预订时长。
        from . import batch_views, boss_views
        from .duration_services import create_cart_orders, create_order

        boss_views.create_order_service = create_order
        batch_views.create_cart_orders = create_cart_orders

        # 订单信号分文件加载。
        from . import cancel_signals, payment_window_signals, room_entry_requeue, signals  # noqa: F401

        # 已付款补位、主动取消处罚与普通抢单统一走同一套入口。
        from . import replacements, services
        from .discipline import can_player_grab_with_discipline
        from apps.players import permission_views, views as player_views

        original_finalize = designations.finalize_lineup_if_full
        original_can_grab = services.can_player_grab_order
        original_grab = services.grab_order
        original_start_timer = services.start_timer

        def finalize_lineup_if_full(order, operator=None, reason='接单人数已满，等待老板付款'):
            result = room_entry_requeue.finalize_lineup_with_paid_replacement(
                original_finalize,
                order,
                operator,
                reason,
            )
            return replacements.finalize_replacement_if_full(result)

        def can_player_grab_order(order, player):
            room_allowed = room_entry_requeue.can_player_grab_after_room_timeout(
                original_can_grab,
                order,
                player,
            )
            if not room_allowed:
                return False
            return can_player_grab_with_discipline(lambda _order, _player: True, order, player)

        def grab_order(order_no, player, operator=None):
            return replacements.grab_order_with_replacement(
                original_grab,
                order_no,
                player,
                operator,
            )

        def start_timer(order, player, operator=None):
            replacements.ensure_order_can_start(order)
            return original_start_timer(order, player, operator)

        designations.finalize_lineup_if_full = finalize_lineup_if_full
        services.finalize_lineup_if_full = finalize_lineup_if_full
        services.can_player_grab_order = can_player_grab_order
        services.grab_order = grab_order
        services.start_timer = start_timer
        permission_views.can_player_grab_order = can_player_grab_order
        permission_views.grab_order_service = grab_order
        player_views.start_timer = start_timer

        # 支付窗口单独作为只读运营审计页面展示。
        from . import payment_window_admin  # noqa: F401
