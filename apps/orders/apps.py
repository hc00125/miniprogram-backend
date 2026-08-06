from django.apps import AppConfig


class OrdersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.orders'
    verbose_name = '订单管理'

    def ready(self):
        # 独立模型必须在应用启动时注册，供迁移、API 和运营后台共同使用。
        from . import cancellation_models, matching_models, payment_window_models  # noqa: F401

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
        from . import cancel_signals, matching, payment_window_signals, room_entry_requeue, signals  # noqa: F401

        # 已付款补位、主动取消处罚与普通抢单统一走同一套入口。
        from rest_framework.exceptions import ValidationError

        from . import replacements, services
        from .discipline import can_player_grab_with_discipline
        from .models import Order
        from apps.players import designation_views, permission_views, views as player_views

        original_finalize = designations.finalize_lineup_if_full
        original_can_grab = services.can_player_grab_order
        original_grab = services.grab_order
        original_start_timer = services.start_timer
        original_complete_order = services.complete_order
        original_create_renewal = boss_views.create_renewal_order
        original_expire_designations = designations.expire_due_designations

        def unresolved_replacement(order):
            queryset = cancellation_models.OrderReplacementState.objects.filter(
                order=order,
            ).exclude(status=cancellation_models.OrderReplacementState.STATUS_RESOLVED)
            # 付款前的普通退出只是继续匹配，不属于“剩余服务待处理”。
            queryset = queryset.exclude(
                mode=cancellation_models.OrderReplacementState.MODE_PUBLIC,
                order__paid=False,
            )
            return queryset.first()

        def finalize_lineup_if_full(order, operator=None, reason='接单人数已满，等待老板付款'):
            result = room_entry_requeue.finalize_lineup_with_paid_replacement(
                original_finalize,
                order,
                operator,
                reason,
            )
            return replacements.finalize_replacement_if_full(result)

        def can_player_grab_order(order, player):
            replacement_state = replacements.open_public_replacement(order)
            if replacement_state:
                room_allowed = room_entry_requeue.can_player_grab_after_room_timeout(
                    lambda _order, _player: True,
                    order,
                    player,
                )
                if not room_allowed:
                    return False
                discipline_allowed = can_player_grab_with_discipline(
                    lambda _order, _player: True,
                    order,
                    player,
                )
                return discipline_allowed and replacements.can_player_take_replacement(
                    order,
                    player,
                    replacement_state,
                )

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

        def complete_order(order, player, operator=None):
            if unresolved_replacement(order):
                raise ValidationError({'detail': '当前仍有陪玩退出或剩余服务待客服处理，暂不能完成订单'})
            return original_complete_order(order, player, operator)

        def create_renewal_order(order_no, user, units):
            order = Order.objects.filter(order_no=order_no).first()
            if order and unresolved_replacement(order):
                raise ValidationError({'detail': '陪玩退出事项处理完成前，暂不能续单'})
            return original_create_renewal(order_no, user, units)

        def expire_due_designations(order=None, now=None):
            return replacements.expire_due_replacement_designations(
                original_expire_designations,
                order=order,
                now=now,
            )

        designations.finalize_lineup_if_full = finalize_lineup_if_full
        designations.expire_due_designations = expire_due_designations
        services.finalize_lineup_if_full = finalize_lineup_if_full
        services.can_player_grab_order = can_player_grab_order
        services.grab_order = grab_order
        services.start_timer = start_timer
        services.complete_order = complete_order
        permission_views.can_player_grab_order = can_player_grab_order
        permission_views.grab_order_service = grab_order
        designation_views.expire_due_designations = expire_due_designations
        player_views.start_timer = start_timer
        player_views.complete_order = complete_order
        boss_views.create_renewal_order = create_renewal_order

        # 独立后台模块只提供审计查看，不允许删除取消记录和补位记录。
        from . import cancellation_admin, payment_window_admin  # noqa: F401
