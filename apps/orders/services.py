import uuid
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.catalog.models import Addon, Package, PackageSpec, PlayerType
from apps.common.money import money
from apps.orders.models import CartItem, Order, OrderItem, OrderPlayer, OrderStatusLog

from .designations import (
    create_designations,
    expire_due_designations,
    finalize_lineup_if_full,
    pending_designation,
    pending_designation_count,
    validate_designated_players,
)


ACTIVE_ORDER_STATUSES = [
    Order.STATUS_WAITING,
    Order.STATUS_PENDING_PAYMENT,
    Order.STATUS_READY_TO_START,
    Order.STATUS_IN_PROGRESS,
]
MAX_CART_BATCH_ORDERS = 20


def generate_order_no():
    return f"{timezone.localtime().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:4].upper()}"


def decimal_value(value):
    return Decimal(str(value or 0))


def ceil_div(value, divisor):
    return (value + divisor - 1) // divisor


def booked_seconds_from_hours(hours):
    return int(decimal_value(hours or 1) * Decimal('3600'))


def normalize_quantity(value):
    try:
        return max(1, min(99, int(value or 1)))
    except (TypeError, ValueError):
        return 1


def append_quantity_note(note, quantity):
    if quantity <= 1:
        return note
    quantity_note = f'购买数量：{quantity}'
    if note:
        return f'{quantity_note}\n{note}'
    return quantity_note


def build_order_note(note, items):
    lines = []
    if len(items) > 1:
        lines.append('合并结算商品：')
        for index, item in enumerate(items, start=1):
            spec_name = item.get('spec_display_name') or item.get('spec_name') or ''
            spec_text = f' / {spec_name}' if spec_name else ''
            lines.append(f"{index}. {item['package'].name}{spec_text} x{item['quantity']} = ¥{money(item['amount'])}")
    elif items and items[0]['quantity'] > 1:
        lines.append(f"购买数量：{items[0]['quantity']}")
    if note:
        lines.append(str(note).strip())
    return '\n'.join([line for line in lines if line]) or None


def normalize_order_items(validated_data):
    raw_items = validated_data.get('items') or []
    if not raw_items:
        raw_items = [{
            'package_id': validated_data.get('package_id'),
            'spec_id': validated_data.get('spec_id'),
            'quantity': validated_data.get('quantity') or 1,
            'spec_display_name': '',
            'image_url': '',
            'description': '',
        }]

    if not raw_items or len(raw_items) > 20:
        raise ValidationError({'detail': '合并结算商品数量不正确'})

    normalized = []
    for index, item in enumerate(raw_items):
        package_id = item.get('package_id')
        if not package_id:
            raise ValidationError({'detail': '商品参数缺失'})
        package = Package.objects.filter(id=package_id, is_active=True).select_related('owner_player').first()
        if not package:
            raise ValidationError({'detail': f'商品 {package_id} 不存在或已下架'})

        spec = None
        spec_id = item.get('spec_id')
        if spec_id:
            spec = PackageSpec.objects.filter(id=spec_id, package=package, is_active=True).first()
            if not spec:
                raise ValidationError({'detail': f'{package.name} 的规格不存在或已下架'})
        quantity = normalize_quantity(item.get('quantity'))
        unit_price = money(spec.price if spec else package.base_price)
        amount = money(unit_price * quantity)
        normalized.append({
            'package': package,
            'spec': spec,
            'quantity': quantity,
            'unit_price': unit_price,
            'amount': amount,
            'spec_name': spec.name if spec else '',
            'spec_display_name': item.get('spec_display_name') or (spec.name if spec else ''),
            'image_url': item.get('image_url') or package.cover_url or package.image_url or package.thumb_url or package.picture_url or '',
            'description': item.get('description') or package.description or '',
            'sort_order': index,
        })
    return normalized


def spec_defines_full_lineup(spec):
    return bool(spec and spec.required_player_type_id)


def is_targeted_product(package):
    return package.selling_mode == Package.SELLING_MODE_PLAYER_DESIGNATED


def validate_targeted_product(package):
    """Validate a one-person product without trusting any player id from the client."""
    player = package.owner_player
    if not player:
        raise ValidationError({'detail': '该陪玩师商品未配置所属陪玩师，暂时无法下单'})
    if package.player_count != 1:
        raise ValidationError({'detail': '陪玩师专属商品必须配置为单人服务'})
    if player.status != player.STATUS_APPROVED or not player.can_be_designated:
        raise ValidationError({'detail': '该陪玩师当前暂不接受指定'})
    if not player.can_accept_orders:
        raise ValidationError({'detail': '该陪玩师当前暂不接单'})
    return player


def ensure_no_active_orders(boss_wechat):
    if Order.objects.filter(boss_wechat=boss_wechat, status__in=ACTIVE_ORDER_STATUSES).exists():
        raise ValidationError({'detail': '您有未完成的订单，请先完成后再下单'})


@transaction.atomic
def create_order(validated_data, user=None, allow_existing_active=False):
    order_items = normalize_order_items(validated_data)
    first_item = order_items[0]
    package = first_item['package']
    spec = first_item['spec']
    targeted_order = is_targeted_product(package)
    target_player = validate_targeted_product(package) if targeted_order else None
    spec_lineup = spec_defines_full_lineup(spec) and not targeted_order

    if targeted_order and len(order_items) != 1:
        raise ValidationError({'detail': '陪玩师专属商品不支持与其他商品合并结算'})
    if targeted_order and (validated_data.get('designated_players') or []):
        raise ValidationError({'detail': '陪玩师已由商品锁定，请勿额外传入指定陪玩'})

    if spec_lineup and len(order_items) != 1:
        raise ValidationError({'detail': '带陪玩类型的规格不能与其他商品合并结算'})
    if spec_lineup and first_item['quantity'] != 1:
        raise ValidationError({'detail': '陪玩类型规格每次只能购买1份，人数由商品默认人数决定'})

    if not allow_existing_active and not targeted_order:
        ensure_no_active_orders(validated_data['boss_wechat'])

    # 带“最低陪玩等级”的固定规格，由商品定义整单人数，后端不接受前端篡改人数。
    required_players = int(package.player_count)
    if required_players <= 0:
        raise ValidationError({'detail': '人数必须大于 0'})

    designated_player_objects = [] if targeted_order else validate_designated_players(
        validated_data.get('designated_players') or [],
        required_players,
    )
    designated_player_ids = [player.id for player in designated_player_objects]

    addon_details = validated_data.get('addon_details') or []
    addon_id = validated_data.get('addon_id')
    if (spec_lineup or targeted_order) and (addon_details or addon_id):
        raise ValidationError({'detail': '已选择陪玩类型规格，不能再叠加特殊陪类型'})

    first_addon = None
    normalized_addons = []
    addon_price = Decimal('0')

    if addon_details:
        for item in addon_details:
            count = int(item.get('count') or 0)
            if count <= 0:
                continue
            addon = Addon.objects.filter(id=item.get('addon_id'), is_active=True).first()
            if not addon:
                raise ValidationError({'detail': f"附加项 {item.get('addon_id')} 不存在"})
            addon_price += decimal_value(addon.price_per_player) * count
            normalized_addons.append({'addon_id': addon.id, 'name': addon.name, 'count': count, 'price': addon.price_per_player})
            first_addon = first_addon or addon
    elif addon_id:
        first_addon = Addon.objects.filter(id=addon_id, is_active=True).first()
        if not first_addon:
            raise ValidationError({'detail': '附加项不存在'})
        addon_price = decimal_value(first_addon.price_per_player) * required_players

    designated_types = []
    total_designated_count = 0
    if spec_lineup:
        # 商品决定人数，规格决定全部名额的最低陪玩等级。
        designated_types.append({
            'type_id': spec.required_player_type_id,
            'count': required_players,
            'source': 'spec',
        })
    else:
        for item in normalized_addons:
            addon = Addon.objects.filter(id=item['addon_id']).first()
            player_type = PlayerType.objects.filter(priority=addon.priority).first() if addon else None
            if player_type:
                designated_types.append({'type_id': player_type.id, 'count': item['count']})
                total_designated_count += item['count']

    if total_designated_count + len(designated_player_ids) > required_players:
        raise ValidationError({'detail': '指定陪玩和特殊陪名额总数不能超过下单人数'})

    subtotal = sum((item['amount'] for item in order_items), Decimal('0'))
    # 商品规格是唯一价格来源；陪玩师专属商品以规格单价 × 服务时长结算。
    total_price = money(subtotal + addon_price)
    booked_hours = first_item['quantity'] if targeted_order else (validated_data.get('booked_hours') or 1.0)
    display_name = package.name if len(order_items) == 1 else f'{package.name}等{len(order_items)}件商品'

    order = Order.objects.create(
        order_no=generate_order_no(),
        boss_user=user if getattr(user, 'is_authenticated', False) else None,
        boss_wechat=validated_data['boss_wechat'],
        game_id=validated_data.get('game_id'),
        package=package,
        spec_id=spec.id if spec else None,
        package_name_snapshot=display_name,
        spec_name_snapshot=spec.name if spec and len(order_items) == 1 else None,
        spec_price_snapshot=spec.price if spec and len(order_items) == 1 else None,
        addon=first_addon,
        addon_details=normalized_addons or None,
        required_players=required_players,
        designated_types=designated_types or None,
        designated_players=designated_player_ids or None,
        boss_note=build_order_note(validated_data.get('boss_note'), order_items),
        total_price_per_hour=first_item['unit_price'] if targeted_order else total_price,
        total_amount=total_price,
        status=Order.STATUS_PENDING_PAYMENT if targeted_order else Order.STATUS_WAITING,
        is_custom=package.is_custom,
        booked_hours=booked_hours,
        order_type=Order.ORDER_TYPE_NORMAL,
        fulfillment_mode=(Order.FULFILLMENT_MODE_TARGETED if targeted_order else Order.FULFILLMENT_MODE_PUBLIC),
        target_player=target_player,
        target_player_name_snapshot=target_player.name if target_player else '',
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
            description=item['description'],
            sort_order=item['sort_order'],
        )

    if designated_player_objects:
        create_designations(order, designated_player_objects)
        names = '、'.join(player.name for player in designated_player_objects)
        reason = f'创建订单并向 {names} 发出指定邀请，其余名额进入抢单大厅'
    elif targeted_order:
        reason = f'创建 {target_player.name} 的专属商品订单，支付成功后将通知该陪玩师确认'
    else:
        reason = '创建订单并自动派单到抢单大厅'
    OrderStatusLog.objects.create(
        order=order,
        to_status=order.status,
        operator=order.boss_user,
        reason=reason,
    )
    return order


@transaction.atomic
def create_cart_orders(cart_item_ids, validated_data, user):
    """将购物车中的每一份商品发布为独立订单，整批成功后再删除购物车项。"""
    ordered_ids = []
    seen_ids = set()
    for raw_id in cart_item_ids or []:
        item_id = int(raw_id)
        if item_id not in seen_ids:
            ordered_ids.append(item_id)
            seen_ids.add(item_id)
    if not ordered_ids:
        raise ValidationError({'detail': '请选择需要结算的购物车商品'})

    locked_items = list(
        CartItem.objects.select_for_update()
        .filter(id__in=ordered_ids, user=user)
        .select_related('package', 'spec')
    )
    item_map = {item.id: item for item in locked_items}
    if len(item_map) != len(ordered_ids):
        raise ValidationError({'detail': '部分购物车商品不存在、已变化或不属于当前账号'})

    ordered_items = [item_map[item_id] for item_id in ordered_ids]
    if any(is_targeted_product(item.package) for item in ordered_items):
        raise ValidationError({'detail': '陪玩师专属商品请从陪玩师详情页直接指定下单，不支持购物车结算'})
    order_count = sum(normalize_quantity(item.quantity) for item in ordered_items)
    if order_count > MAX_CART_BATCH_ORDERS:
        raise ValidationError({'detail': f'单次最多发布 {MAX_CART_BATCH_ORDERS} 个独立订单'})

    ensure_no_active_orders(validated_data['boss_wechat'])
    orders = []
    for cart_item in ordered_items:
        for _ in range(normalize_quantity(cart_item.quantity)):
            order_payload = {
                'boss_wechat': validated_data['boss_wechat'],
                'game_id': validated_data.get('game_id'),
                'package_id': cart_item.package_id,
                'spec_id': cart_item.spec_id,
                'quantity': 1,
                'required_players': cart_item.package.player_count,
                'addon_details': None,
                'designated_players': None,
                'boss_note': validated_data.get('boss_note'),
                'booked_hours': validated_data.get('booked_hours') or 1.0,
            }
            orders.append(create_order(order_payload, user, allow_existing_active=True))

    CartItem.objects.filter(id__in=ordered_ids, user=user).delete()
    return orders


def remaining_type_slots(order):
    remaining = []
    pending_concrete = pending_designation_count(order)
    for item in order.designated_types or []:
        type_id = item.get('type_id')
        count = int(item.get('count') or 0)
        if not type_id or count <= 0:
            continue

        if item.get('source') == 'spec':
            player_type = PlayerType.objects.filter(id=type_id).first()
            if not player_type:
                continue
            # 规格定义的是整单最低等级。具体指定邀请同样已通过规格等级校验，
            # 因此待接受邀请占用对应名额；拒绝或超时后名额会自动重新开放。
            filled = order.order_players.filter(
                player__player_type__priority__gte=player_type.priority,
            ).count()
            open_count = max(0, count - filled - pending_concrete)
        else:
            filled = order.order_players.filter(designated_type_id=type_id).count()
            open_count = max(0, count - filled)

        if open_count:
            remaining.append({'type_id': type_id, 'count': open_count, 'source': item.get('source')})
    return remaining


def can_player_grab_order(order, player):
    if order.fulfillment_mode == Order.FULFILLMENT_MODE_TARGETED:
        return False
    expire_due_designations(order=order)
    if order.order_players.filter(player=player).exists():
        return False
    if pending_designation(order, player):
        return False

    current_players = order.order_players.count()
    if current_players >= order.required_players:
        return False

    pending_concrete = pending_designation_count(order)
    type_slots = remaining_type_slots(order)
    remaining_type_count = sum(item['count'] for item in type_slots)
    public_slots = max(0, order.required_players - current_players - pending_concrete - remaining_type_count)

    for item in type_slots:
        player_type = PlayerType.objects.filter(id=item['type_id']).first()
        if player_type and player.player_type.priority >= player_type.priority:
            return True
    return public_slots > 0


def assign_designated_slot(order, player):
    eligible = []
    for item in remaining_type_slots(order):
        player_type = PlayerType.objects.filter(id=item.get('type_id')).first()
        if player_type and player.player_type.priority >= player_type.priority:
            eligible.append((int(player_type.priority or 0), item.get('type_id')))
    if not eligible:
        return False, None
    _, designated_type_id = max(eligible, key=lambda value: value[0])
    return True, designated_type_id


@transaction.atomic
def grab_order(order_no, player, operator=None):
    order = Order.objects.select_for_update(of=('self',)).select_related('package', 'addon').get(order_no=order_no)
    expire_due_designations(order=order)
    if (
        order.status != Order.STATUS_WAITING
        or order.order_type != Order.ORDER_TYPE_NORMAL
        or order.fulfillment_mode != Order.FULFILLMENT_MODE_PUBLIC
    ):
        raise ValidationError({'detail': '订单已被抢或状态已变更'})
    if order.order_players.filter(player=player).exists():
        raise ValidationError({'detail': '您已经接了这个订单'})
    if pending_designation(order, player):
        raise ValidationError({'detail': '这是老板给您的指定邀请，请点击“接受指定”'})
    if order.order_players.count() >= order.required_players:
        raise ValidationError({'detail': '订单已满员'})
    if not can_player_grab_order(order, player):
        raise ValidationError({'detail': '没有您可以抢的公开位置'})

    is_designated, designated_type_id = assign_designated_slot(order, player)
    OrderPlayer.objects.create(order=order, player=player, is_designated=is_designated, designated_type_id=designated_type_id)
    player.total_orders = (player.total_orders or 0) + 1
    player.save(update_fields=['total_orders'])
    return finalize_lineup_if_full(order, operator)


def ensure_order_player(order, player):
    if not order.order_players.filter(player=player).exists():
        raise ValidationError({'detail': '您不是这个订单的打手'})


@transaction.atomic
def start_timer(order, player, operator=None):
    ensure_order_player(order, player)
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.order_type != Order.ORDER_TYPE_NORMAL:
        raise ValidationError({'detail': '续单不单独开打'})
    if order.status != Order.STATUS_READY_TO_START:
        raise ValidationError({'detail': '订单尚未付款或已开始，当前状态不能开打'})
    if not order.paid:
        raise ValidationError({'detail': '老板尚未完成付款'})
    if order.timer_started_at:
        raise ValidationError({'detail': '计时已经开始了'})

    old_status = order.status
    now = timezone.now()
    order.status = Order.STATUS_IN_PROGRESS
    order.start_time = now
    order.timer_started_at = now
    order.end_time = None
    order.duration_minutes = None
    order.paused_duration = 0
    order.is_paused = False
    order.last_paused_at = None
    order.save(update_fields=[
        'status', 'start_time', 'timer_started_at', 'end_time', 'duration_minutes',
        'paused_duration', 'is_paused', 'last_paused_at',
    ])
    OrderStatusLog.objects.create(
        order=order,
        from_status=old_status,
        to_status=order.status,
        operator=operator,
        reason='陪玩确认开打并开始计时',
    )
    return order


def pause_order(order):
    if order.status != Order.STATUS_IN_PROGRESS or not order.timer_started_at:
        raise ValidationError({'detail': '计时未开始'})
    if order.is_paused:
        raise ValidationError({'detail': '已经暂停了'})
    order.is_paused = True
    order.last_paused_at = timezone.now()
    order.save(update_fields=['is_paused', 'last_paused_at'])
    return order


def resume_order(order):
    if order.status != Order.STATUS_IN_PROGRESS or not order.timer_started_at:
        raise ValidationError({'detail': '计时未开始'})
    if not order.is_paused:
        raise ValidationError({'detail': '计时未暂停'})
    if order.last_paused_at:
        paused_seconds = int((timezone.now() - order.last_paused_at).total_seconds())
        order.paused_duration = (order.paused_duration or 0) + paused_seconds
    order.is_paused = False
    order.last_paused_at = None
    order.save(update_fields=['paused_duration', 'is_paused', 'last_paused_at'])
    return order


@transaction.atomic
def complete_order(order, player, operator=None):
    ensure_order_player(order, player)
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.order_type != Order.ORDER_TYPE_NORMAL:
        raise ValidationError({'detail': '续单不单独完成'})
    if order.status != Order.STATUS_IN_PROGRESS:
        raise ValidationError({'detail': '订单状态不允许完成'})
    if order.renewal_orders.filter(status=Order.STATUS_PENDING_PAYMENT, paid=False).exists():
        raise ValidationError({'detail': '当前还有待支付续单，请先完成或取消续单后再结束服务'})

    op = order.order_players.get(player=player)
    op.status = '已完成'
    op.save(update_fields=['status'])

    all_completed = not order.order_players.exclude(status='已完成').exists()
    if all_completed:
        old_status = order.status
        now = timezone.now()
        order.status = Order.STATUS_COMPLETED
        order.end_time = now
        if order.is_paused and order.last_paused_at:
            paused_seconds = int((now - order.last_paused_at).total_seconds())
            order.paused_duration = (order.paused_duration or 0) + paused_seconds
            order.is_paused = False
            order.last_paused_at = None
        if order.timer_started_at:
            total_seconds = int((now - order.timer_started_at).total_seconds())
            effective_seconds = max(0, total_seconds - (order.paused_duration or 0))
            order.duration_minutes = max(1, ceil_div(effective_seconds, 60))
        elif order.start_time:
            order.duration_minutes = max(1, ceil_div(int((now - order.start_time).total_seconds()), 60))
        order.save(update_fields=[
            'status', 'end_time', 'duration_minutes', 'paused_duration',
            'is_paused', 'last_paused_at',
        ])
        OrderStatusLog.objects.create(
            order=order,
            from_status=old_status,
            to_status=order.status,
            operator=operator,
            reason='所有陪玩已完成服务',
        )
    return order


def cancel_order(order, reason=None, operator=None):
    if order.status not in {Order.STATUS_WAITING, Order.STATUS_PENDING_PAYMENT}:
        raise ValidationError({'detail': '已付款或已开打订单请联系管理员处理退款，不能直接取消'})
    old_status = order.status
    order.status = Order.STATUS_CANCELLED
    order.canceled_at = timezone.now()
    order.cancel_reason = reason
    order.save(update_fields=['status', 'canceled_at', 'cancel_reason'])
    order.designations.filter(status='pending').update(status='cancelled', responded_at=timezone.now())
    OrderStatusLog.objects.create(order=order, from_status=old_status, to_status=order.status, operator=operator, reason=reason or '取消订单')
    return order
