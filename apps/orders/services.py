import math
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.catalog.models import Addon, Package, PlayerType
from apps.common.money import money
from apps.orders.models import Order, OrderPlayer, OrderStatusLog
from apps.players.models import Player


def generate_order_no():
    return f"{timezone.localtime().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:4].upper()}"


def create_order(validated_data, user=None):
    package = Package.objects.filter(id=validated_data['package_id'], is_active=True).first()
    if not package:
        raise ValidationError({'detail': '套餐不存在'})

    # 检查该老板是否有未完成的订单
    active_statuses = [Order.STATUS_WAITING, Order.STATUS_IN_PROGRESS, Order.STATUS_PENDING_PAYMENT]
    has_active = Order.objects.filter(
        boss_wechat=validated_data['boss_wechat'],
        status__in=active_statuses
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
    addon_price = 0

    if addon_details:
        for item in addon_details:
            count = int(item.get('count') or 0)
            if count <= 0:
                continue
            addon = Addon.objects.filter(id=item.get('addon_id'), is_active=True).first()
            if not addon:
                raise ValidationError({'detail': f"附加项 {item.get('addon_id')} 不存在"})
            addon_price += float(addon.price_per_player) * count
            normalized_addons.append({'addon_id': addon.id, 'name': addon.name, 'count': count, 'price': addon.price_per_player})
            first_addon = first_addon or addon
    elif addon_id:
        first_addon = Addon.objects.filter(id=addon_id, is_active=True).first()
        if not first_addon:
            raise ValidationError({'detail': '附加项不存在'})
        addon_price = float(first_addon.price_per_player) * required_players

    designated_players = validated_data.get('designated_players') or []
    if len(designated_players) > required_players:
        raise ValidationError({'detail': '指定打手人数不能超过下单人数'})

    player_type_extra = 0
    for player_id in designated_players:
        player = Player.objects.select_related('player_type').filter(id=player_id).first()
        if player:
            player_type_extra += float(player.player_type.price_extra or 0)

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

    total_price = money(float(package.base_price) * required_players + addon_price + player_type_extra)
    booked_hours = validated_data.get('booked_hours') or 1.0

    order = Order.objects.create(
        order_no=generate_order_no(),
        boss_user=user if getattr(user, 'is_authenticated', False) else None,
        boss_wechat=validated_data['boss_wechat'],
        game_id=validated_data.get('game_id'),
        package=package,
        addon=first_addon,
        addon_details=normalized_addons or None,
        required_players=required_players,
        designated_types=designated_types or None,
        designated_players=designated_players or None,
        boss_note=validated_data.get('boss_note'),
        total_price_per_hour=total_price,
        total_amount=total_price,
        status=Order.STATUS_WAITING,
        is_custom=package.is_custom,
        booked_hours=booked_hours,
    )
    OrderStatusLog.objects.create(order=order, to_status=order.status, operator=order.boss_user, reason='创建订单')
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
    order = Order.objects.select_for_update().select_related('package', 'addon').get(order_no=order_no)
    if order.status != Order.STATUS_WAITING:
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
        order.status = Order.STATUS_IN_PROGRESS
        order.start_time = timezone.now()
        order.save(update_fields=['status', 'start_time'])
        OrderStatusLog.objects.create(order=order, from_status=old_status, to_status=order.status, operator=operator, reason='接单满员')
    return order


def ensure_order_player(order, player):
    if not order.order_players.filter(player=player).exists():
        raise ValidationError({'detail': '您不是这个订单的打手'})


def start_timer(order, player):
    ensure_order_player(order, player)
    if order.status != Order.STATUS_IN_PROGRESS:
        raise ValidationError({'detail': '订单状态不允许开始计时'})
    if order.timer_started_at:
        raise ValidationError({'detail': '计时已经开始了'})
    order.timer_started_at = timezone.now()
    order.save(update_fields=['timer_started_at'])
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


def complete_order(order, player, operator=None):
    ensure_order_player(order, player)
    if order.status != Order.STATUS_IN_PROGRESS:
        raise ValidationError({'detail': '订单状态不允许完成'})
    op = order.order_players.get(player=player)
    op.status = '已完成'
    op.save(update_fields=['status'])

    all_completed = not order.order_players.exclude(status='已完成').exists()
    if all_completed:
        old_status = order.status
        now = timezone.now()
        order.status = Order.STATUS_PENDING_PAYMENT
        order.end_time = now
        if order.is_paused and order.last_paused_at:
            paused_seconds = int((now - order.last_paused_at).total_seconds())
            order.paused_duration = (order.paused_duration or 0) + paused_seconds
            order.is_paused = False
            order.last_paused_at = None
        if order.timer_started_at:
            total_seconds = (now - order.timer_started_at).total_seconds()
            effective_seconds = max(0, total_seconds - (order.paused_duration or 0))
            order.duration_minutes = max(1, int(effective_seconds / 60))
            booked_minutes = (order.booked_hours or 1.0) * 60
            extra_minutes = effective_seconds / 60 - booked_minutes
            base_amount = order.total_price_per_hour or 0
            if extra_minutes > 29:
                extra_half_hours = math.ceil((extra_minutes - 29) / 30)
                order.total_amount = money(base_amount + extra_half_hours * (base_amount * 0.5))
            else:
                order.total_amount = money(base_amount)
        elif order.start_time:
            order.duration_minutes = max(1, int((now - order.start_time).total_seconds() / 60))
            order.total_amount = order.total_amount or order.total_price_per_hour
        else:
            order.total_amount = order.total_amount or order.total_price_per_hour
        order.save()
        OrderStatusLog.objects.create(order=order, from_status=old_status, to_status=order.status, operator=operator, reason='服务完成')
    return order


def cancel_order(order, reason=None, operator=None):
    if order.status not in {Order.STATUS_WAITING, Order.STATUS_IN_PROGRESS}:
        raise ValidationError({'detail': '当前状态无法取消'})
    old_status = order.status
    order.status = Order.STATUS_CANCELLED
    order.canceled_at = timezone.now()
    order.cancel_reason = reason
    order.save(update_fields=['status', 'canceled_at', 'cancel_reason'])
    OrderStatusLog.objects.create(order=order, from_status=old_status, to_status=order.status, operator=operator, reason=reason or '取消订单')
    return order
