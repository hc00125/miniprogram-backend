from decimal import Decimal

from django.db import transaction
from rest_framework.exceptions import ValidationError

from apps.common.money import money
from apps.orders.models import Order, OrderItem, OrderStatusLog
from apps.players.models import Player, PlayerServiceListing

from .services import build_order_note, generate_order_no, normalize_order_items


@transaction.atomic
def create_listing_order(validated_data, listing_id, user=None):
    """Create a targeted order without making the shared package player-owned."""
    listing = (
        PlayerServiceListing.objects
        .select_for_update()
        .select_related('player__player_type', 'spec__package')
        .filter(pk=listing_id)
        .first()
    )
    if not listing:
        raise ValidationError({'detail': '陪玩师服务已下架或不存在，请重新选择'})
    if listing.status != PlayerServiceListing.STATUS_APPROVED or not listing.is_available:
        raise ValidationError({'detail': '陪玩师服务尚未审核通过或当前不可预约'})

    player = listing.player
    if player.status != Player.STATUS_APPROVED or not player.can_be_designated:
        raise ValidationError({'detail': '该陪玩师当前暂不接受指定'})
    if not player.can_accept_orders:
        raise ValidationError({'detail': '该陪玩师当前暂不接单'})

    order_items = normalize_order_items(validated_data)
    if len(order_items) != 1:
        raise ValidationError({'detail': '指定陪玩服务不支持与其他商品合并结算'})
    first_item = order_items[0]
    package = first_item['package']
    spec = first_item['spec']
    if not spec or spec.id != listing.spec_id or package.id != listing.spec.package_id:
        raise ValidationError({'detail': '陪玩师上架记录与所选商品规格不一致，请重新选择'})
    if package.player_count != 1:
        raise ValidationError({'detail': '当前指定陪玩服务只支持单人规格'})
    if validated_data.get('designated_players'):
        raise ValidationError({'detail': '陪玩师已由上架记录锁定，请勿额外传入指定陪玩'})
    if validated_data.get('addon_details') or validated_data.get('addon_id'):
        raise ValidationError({'detail': '指定陪玩服务暂不支持叠加特殊陪类型'})

    total_price = money(sum((item['amount'] for item in order_items), Decimal('0')))
    booked_hours = first_item['quantity']
    order = Order.objects.create(
        order_no=generate_order_no(),
        boss_user=user if getattr(user, 'is_authenticated', False) else None,
        boss_wechat=validated_data['boss_wechat'],
        game_id=validated_data.get('game_id'),
        package=package,
        spec_id=spec.id,
        package_name_snapshot=package.name,
        spec_name_snapshot=spec.name,
        spec_price_snapshot=spec.price,
        required_players=1,
        designated_types=None,
        designated_players=None,
        boss_note=build_order_note(validated_data.get('boss_note'), order_items),
        total_price_per_hour=first_item['unit_price'],
        total_amount=total_price,
        status=Order.STATUS_PENDING_PAYMENT,
        is_custom=package.is_custom,
        booked_hours=booked_hours,
        order_type=Order.ORDER_TYPE_NORMAL,
        fulfillment_mode=Order.FULFILLMENT_MODE_TARGETED,
        target_player=player,
        target_player_name_snapshot=player.name,
    )

    for item in order_items:
        OrderItem.objects.create(
            order=order,
            package=item['package'],
            spec=item['spec'],
            package_name=item['package'].name,
            spec_name=item['spec_name'],
            spec_display_name=item['spec_display_name'],
            unit_price=item['unit_price'],
            quantity=item['quantity'],
            amount=item['amount'],
            image_url=item['image_url'],
            description=listing.custom_description or item['description'],
            sort_order=item['sort_order'],
        )

    OrderStatusLog.objects.create(
        order=order,
        to_status=order.status,
        operator=order.boss_user,
        reason=f'通过共享规格创建 {player.name} 的指定服务订单，支付成功后通知该陪玩师确认',
    )
    return order
