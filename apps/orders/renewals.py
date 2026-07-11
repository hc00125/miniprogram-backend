import uuid
from decimal import Decimal

from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.common.money import money

from .models import Order, OrderItem, OrderPlayer, OrderStatusLog


MAX_RENEWAL_UNITS = 10


def generate_renewal_order_no():
    return f"{timezone.localtime().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:4].upper()}"


def root_order(order):
    if order.order_type == Order.ORDER_TYPE_RENEWAL and order.parent_order_id:
        return order.parent_order
    return order


def paid_renewal_orders(order):
    root = root_order(order)
    return root.renewal_orders.filter(
        order_type=Order.ORDER_TYPE_RENEWAL,
        paid=True,
        status=Order.STATUS_COMPLETED,
    ).order_by('renewal_index', 'id')


def renewal_totals(order):
    root = root_order(order)
    renewals = list(paid_renewal_orders(root))
    renewal_hours = sum((Decimal(str(item.booked_hours or 0)) for item in renewals), Decimal('0'))
    renewal_amount = sum((money(item.total_amount) for item in renewals), Decimal('0'))
    original_hours = Decimal(str(root.booked_hours or 0))
    return {
        'renewal_count': len(renewals),
        'renewal_booked_hours': float(renewal_hours),
        'renewal_paid_amount': float(renewal_amount),
        'total_booked_hours': float(original_hours + renewal_hours),
    }


def ensure_owner(order, user):
    if not user or not getattr(user, 'is_authenticated', False):
        raise PermissionDenied('请先登录')
    if order.boss_user_id != user.id and not getattr(user, 'is_staff', False):
        raise PermissionDenied('无权为该订单续单')


def validate_renewable_order(root):
    if root.order_type != Order.ORDER_TYPE_NORMAL:
        raise ValidationError({'detail': '只能对原订单发起续单'})
    if root.status not in {Order.STATUS_READY_TO_START, Order.STATUS_IN_PROGRESS}:
        raise ValidationError({'detail': '只有已付款待开打或服务中的订单可以续单'})
    if not root.paid:
        raise ValidationError({'detail': '原订单尚未付款，不能续单'})


def validate_fixed_price_items(root, units):
    items = list(root.items.select_related('package', 'spec').order_by('sort_order', 'id'))
    if len(items) != 1:
        raise ValidationError({'detail': '当前续单仅支持单个固定价格商品，合并商品订单暂不能续单'})

    item = items[0]
    item_total = money(item.amount)
    root_amount = money(root.total_amount if root.total_amount is not None else root.total_price_per_hour)
    if root_amount <= 0:
        raise ValidationError({'detail': '原订单金额不正确'})
    if item_total != root_amount:
        raise ValidationError({
            'detail': '当前续单仅支持无附加项、无指定陪玩加价的固定价格订单'
        })

    quantity = int(item.quantity or 1) * units
    if quantity > 99:
        raise ValidationError({'detail': '续单后的商品数量不能超过99'})
    return item, root_amount


@transaction.atomic
def create_renewal_order(order_no, user, units=1):
    try:
        units = int(units or 1)
    except (TypeError, ValueError) as exc:
        raise ValidationError({'detail': '续单份数不正确'}) from exc
    if units < 1 or units > MAX_RENEWAL_UNITS:
        raise ValidationError({'detail': f'续单份数只能为1到{MAX_RENEWAL_UNITS}'})

    requested = (
        Order.objects
        .select_related('parent_order', 'package', 'addon', 'boss_user')
        .filter(order_no=order_no)
        .first()
    )
    if not requested:
        raise ValidationError({'detail': '订单不存在'})

    root_id = requested.parent_order_id or requested.id
    root = (
        Order.objects
        .select_for_update()
        .select_related('package', 'addon', 'boss_user')
        .get(pk=root_id)
    )
    ensure_owner(root, user)
    validate_renewable_order(root)

    existing = (
        root.renewal_orders
        .filter(status=Order.STATUS_PENDING_PAYMENT, paid=False)
        .order_by('-created_at')
        .first()
    )
    if existing:
        return existing, False

    item, unit_amount = validate_fixed_price_items(root, units)
    order_players = list(root.order_players.select_related('player').order_by('id'))
    if len(order_players) != root.required_players:
        raise ValidationError({'detail': '原订单陪玩阵容不完整，暂不能续单'})

    next_index = (
        root.renewal_orders.aggregate(max_index=Max('renewal_index')).get('max_index') or 0
    ) + 1
    renewal_hours = Decimal(str(root.booked_hours or 1)) * units
    renewal_amount = money(unit_amount * units)

    renewal = Order.objects.create(
        order_no=generate_renewal_order_no(),
        boss_user=root.boss_user,
        boss_wechat=root.boss_wechat,
        game_id=root.game_id,
        package=root.package,
        spec_id=root.spec_id,
        package_name_snapshot=root.package_name_snapshot,
        spec_name_snapshot=root.spec_name_snapshot,
        spec_price_snapshot=root.spec_price_snapshot,
        addon=None,
        addon_details=None,
        required_players=root.required_players,
        designated_types=root.designated_types,
        designated_players=root.designated_players,
        boss_note=f'续单自 {root.order_no}，第 {next_index} 次续单',
        total_price_per_hour=float(unit_amount),
        total_amount=float(renewal_amount),
        status=Order.STATUS_PENDING_PAYMENT,
        paid=False,
        is_custom=root.is_custom,
        custom_price=root.custom_price,
        booked_hours=float(renewal_hours),
        kook_room_number=root.kook_room_number,
        kook_room_updated_at=root.kook_room_updated_at,
        kook_room_updated_by=root.kook_room_updated_by,
        order_type=Order.ORDER_TYPE_RENEWAL,
        parent_order=root,
        renewal_index=next_index,
    )

    OrderItem.objects.create(
        order=renewal,
        package=item.package,
        spec=item.spec,
        package_name=item.package_name,
        spec_name=item.spec_name,
        spec_display_name=item.spec_display_name,
        unit_price=item.unit_price,
        quantity=int(item.quantity or 1) * units,
        amount=float(money(money(item.amount) * units)),
        image_url=item.image_url,
        description=item.description,
        sort_order=item.sort_order,
    )

    for order_player in order_players:
        OrderPlayer.objects.create(
            order=renewal,
            player=order_player.player,
            is_designated=order_player.is_designated,
            designated_type_id=order_player.designated_type_id,
            status='已接单',
        )

    OrderStatusLog.objects.create(
        order=renewal,
        to_status=renewal.status,
        operator=user if getattr(user, 'is_authenticated', False) else None,
        reason=f'由原订单 {root.order_no} 创建第 {next_index} 次续单，等待付款',
    )
    OrderStatusLog.objects.create(
        order=root,
        from_status=root.status,
        to_status=root.status,
        operator=user if getattr(user, 'is_authenticated', False) else None,
        reason=f'已创建续单 {renewal.order_no}，增加 {float(renewal_hours):g} 小时，等待付款',
    )
    return renewal, True


@transaction.atomic
def finalize_paid_renewal(order, paid_at=None, operator=None):
    if order.order_type != Order.ORDER_TYPE_RENEWAL:
        return order

    paid_at = paid_at or timezone.now()
    renewal = Order.objects.select_for_update().get(pk=order.pk)
    if renewal.status == Order.STATUS_COMPLETED:
        return renewal
    if not renewal.parent_order_id:
        raise ValidationError({'detail': '续单缺少原订单关联'})

    # 锁定原订单，与“完成服务”操作串行化，避免续单付款和结束订单同时发生。
    parent = Order.objects.select_for_update().get(pk=renewal.parent_order_id)

    old_status = renewal.status
    renewal.status = Order.STATUS_COMPLETED
    renewal.end_time = paid_at
    renewal.duration_minutes = max(1, int(round(float(renewal.booked_hours or 0) * 60)))
    renewal.save(update_fields=['status', 'end_time', 'duration_minutes'])

    OrderStatusLog.objects.create(
        order=renewal,
        from_status=old_status,
        to_status=renewal.status,
        operator=operator,
        reason='续单付款成功，续单时长已计入原订单',
    )
    OrderStatusLog.objects.create(
        order=parent,
        from_status=parent.status,
        to_status=parent.status,
        operator=operator,
        reason=(
            f'续单 {renewal.order_no} 付款成功，累计增加 '
            f'{float(renewal.booked_hours or 0):g} 小时'
        ),
    )
    return renewal
