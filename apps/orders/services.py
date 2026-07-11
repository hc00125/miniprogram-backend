import uuid
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.catalog.models import Addon, Package, PackageSpec, PlayerType
from apps.common.money import money
from apps.orders.models import Order, OrderItem, OrderPlayer, OrderStatusLog
from apps.players.models import Player


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
        package = Package.objects.filter(id=package_id, is_active=True).first()
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


@transaction.atomic
def create_order(validated_data, user=None):
    order_items = normalize_order_items(validated_data)
    first_item = order_items[0]
    package = first_item['package']
    spec = first_item['spec']

    active_statuses = [
        Order.STATUS_WAITING,
        Order.STATUS_PENDING_PAYMENT,
        Order.STATUS_READY_TO_START,
        Order.STATUS_IN_PROGRESS,
    ]
    has_active = Order.objects.filter(
        boss_wechat=validated_data['boss_wechat'],
        status__in=active_statuses,
    ).exists()
    if has_active:
        raise ValidationError({'detail': '您有未完成的订单，请先完成后再下单'})

    required_players = int(validated_data.get('required_players') or package.player_count)
    if required_players <= 0:
        raise ValidationError({'detail': '人数必须大于 0'})

    addon_details = validated_data.get('addon_details') or []
    addon_id = validated_data.get('addon_id')
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

    designated_players = validated_data.get('designated_players') or []
    if len(designated_players) > required_players:
        raise ValidationError({'detail': '指定打手人数不能超过下单人数'})

    player_type_extra = Decimal('0')
    for player_id in designated_players:
        player = Player.objects.select_related('player_type').filter(id=player_id).first()
        if player:
            player_type_extra += decimal_value(player.player_type.price_extra or 0)

    designated_types = []
    total_designated_count = 0
    for item in normalized_addons:
        addon = Addon.objects.filter(id=item['addon_id']).first()
        player_type = PlayerType.objects.filter(priority=addon.priority).first() if addon else None
        if player_type:
            designated_types.append({'type_id': player_type.id, 'count': item['count']})
            total_designated_count += item['count']

    if total_designated_count > required_players:
        raise ValidationError({'detail': '特殊陪数量不能超过下单人数'})

    for player_id in designated_players:
        player = Player.objects.filter(id=player_id).first()
        if not player:
            continue
        existing = next((item for item in designated_types if item['type_id'] == player.player_type_id), None)
        if existing:
            existing['count'] += 1
        else:
            designated_types.append({'type_id': player.player_type_id, 'count': 1})

    subtotal = sum((item['amount'] for item in order_items), Decimal('0'))
    total_price = money(subtotal + addon_price + player_type_extra)
    booked_hours = validated_data.get('booked_hours') or 1.0
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
        designated_players=designated_players or None,
        boss_note=build_order_note(validated_data.get('boss_note'), order_items),
        total_price_per_hour=total_price,
        total_amount=total_price,
        status=Order.STATUS_WAITING,
        is_custom=package.is_custom,
        booked_hours=booked_hours,
        order_type=Order.ORDER_TYPE_NORMAL,
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

    OrderStatusLog.objects.create(
        order=order,
        to_status=order.status,
        operator=order.boss_user,
        reason='创建订单并自动派单到抢单大厅',
    )
    return order


def can_player_grab_order(order, player):
    if order.order_players.filter(player=player).exists():
        return False
    current_players = order.order_players.count()
    if current_players >= order.required_players:
        return False
    if order.designated_players and player.id in order.designated_players:
        return True
    if not order.designated_types:
        return True

    total_designated_slots = sum(item.get('count', 0) for item in order.designated_types)
    open_slots = order.required_players - total_designated_slots
    grabbed_designated = order.order_players.exclude(designated_type_id__isnull=True).count()
    grabbed_open = current_players - grabbed_designated
    remaining_open = max(0, open_slots - grabbed_open)

    for item in order.designated_types:
        player_type = PlayerType.objects.filter(id=item.get('type_id')).first()
        if not player_type or player.player_type.priority < player_type.priority:
            continue
        filled = order.order_players.filter(designated_type_id=item.get('type_id')).count()
        if filled < item.get('count', 0):
            return True
    return remaining_open > 0


def assign_designated_slot(order, player):
    is_designated = bool(order.designated_players and player.id in order.designated_players)
    designated_type_id = None
    if order.designated_types:
        for item in sorted(order.designated_types, key=lambda value: value.get('type_id', 0), reverse=True):
            player_type = PlayerType.objects.filter(id=item.get('type_id')).first()
            if not player_type or player.player_type.priority < player_type.priority:
                continue
            filled = order.order_players.filter(designated_type_id=item.get('type_id')).count()
            if filled < item.get('count', 0):
                is_designated = True
                designated_type_id = item.get('type_id')
                break
    return is_designated, designated_type_id


@transaction.atomic
def grab_order(order_no, player, operator=None):
    order = Order.objects.select_for_update(of=('self',)).select_related('package', 'addon').get(order_no=order_no)
    if order.status != Order.STATUS_WAITING or order.order_type != Order.ORDER_TYPE_NORMAL:
        raise ValidationError({'detail': '订单已被抢或状态已变更'})
    if order.order_players.filter(player=player).exists():
        raise ValidationError({'detail': '您已经接了这个订单'})
    if order.order_players.count() >= order.required_players:
        raise ValidationError({'detail': '订单已满员'})
    if not can_player_grab_order(order, player):
        raise ValidationError({'detail': '没有您可以抢的位置'})

    is_designated, designated_type_id = assign_designated_slot(order, player)
    OrderPlayer.objects.create(order=order, player=player, is_designated=is_designated, designated_type_id=designated_type_id)
    player.total_orders = (player.total_orders or 0) + 1
    player.save(update_fields=['total_orders'])

    if order.order_players.count() >= order.required_players:
        old_status = order.status
        order.status = Order.STATUS_PENDING_PAYMENT
        order.save(update_fields=['status'])
        OrderStatusLog.objects.create(
            order=order,
            from_status=old_status,
            to_status=order.status,
            operator=operator,
            reason='接单人数已满，等待老板付款',
        )
    return order


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
    OrderStatusLog.objects.create(order=order, from_status=old_status, to_status=order.status, operator=operator, reason=reason or '取消订单')
    return order
