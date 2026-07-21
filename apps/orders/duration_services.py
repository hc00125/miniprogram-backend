from copy import deepcopy
from decimal import Decimal, InvalidOperation

from django.db import transaction
from rest_framework.exceptions import ValidationError

from apps.catalog.models import Package
from apps.common.money import money

from .models import CartItem
from .services import create_order as create_order_base
from .services import ensure_no_active_orders


MAX_SERVICE_HOURS = 24
MAX_CART_ORDER_ITEMS = 20


def is_hourly_service(package):
    """除保底单外，当前小程序商品数量统一表示服务小时数。"""
    return package.product_type != Package.PRODUCT_TYPE_GUARANTEE


def normalize_service_hours(value):
    try:
        parsed = Decimal(str(value if value is not None else 1))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({'detail': '服务时长不正确'}) from exc

    if parsed != parsed.to_integral_value():
        raise ValidationError({'detail': '服务时长必须为整小时'})

    hours = int(parsed)
    if hours < 1 or hours > MAX_SERVICE_HOURS:
        raise ValidationError({'detail': f'单次服务时长只能选择1至{MAX_SERVICE_HOURS}小时'})
    return hours


def _single_item_payload(validated_data):
    items = validated_data.get('items') or []
    if items:
        if len(items) != 1:
            raise ValidationError({'detail': '服务时长订单每次只能结算一个商品'})
        return items[0]
    return validated_data


def _resolve_package(validated_data):
    item = _single_item_payload(validated_data)
    package_id = item.get('package_id')
    if not package_id:
        raise ValidationError({'detail': '商品参数缺失'})
    package = Package.objects.filter(id=package_id, is_active=True).first()
    if not package:
        raise ValidationError({'detail': '商品不存在或已下架'})
    return package, item


def _force_base_quantity(payload, hours):
    prepared = deepcopy(payload)
    items = prepared.get('items') or []
    if items:
        items[0]['quantity'] = 1
        prepared['items'] = items
    else:
        prepared['quantity'] = 1
    prepared['booked_hours'] = float(hours)
    return prepared


def _append_duration_note(note, hours):
    duration_line = f'预订时长：{hours}小时'
    text = str(note or '').strip()
    if duration_line in text:
        return text
    return f'{duration_line}\n{text}' if text else duration_line


def _finalize_hourly_order(order, hours):
    per_hour = money(
        order.total_price_per_hour
        if order.total_price_per_hour is not None
        else order.total_amount
    )
    total_amount = money(per_hour * Decimal(hours))

    order.total_price_per_hour = float(per_hour)
    order.total_amount = float(total_amount)
    order.booked_hours = float(hours)
    order.boss_note = _append_duration_note(order.boss_note, hours)
    order.save(update_fields=[
        'total_price_per_hour',
        'total_amount',
        'booked_hours',
        'boss_note',
    ])

    item = order.items.order_by('sort_order', 'id').first()
    if item:
        item.quantity = hours
        item.amount = float(money(Decimal(str(item.unit_price or 0)) * Decimal(hours)))
        item.save(update_fields=['quantity', 'amount'])
    return order


@transaction.atomic
def create_order(validated_data, user=None, allow_existing_active=False):
    """创建一张业务订单；小时制商品的 quantity 表示 booked_hours。"""
    package, item = _resolve_package(validated_data)
    requested_quantity = normalize_service_hours(item.get('quantity') or 1)
    booked_hours_raw = validated_data.get('booked_hours')

    if not is_hourly_service(package):
        if requested_quantity != 1:
            raise ValidationError({'detail': '该商品按单收费，每次只能购买1份'})
        if booked_hours_raw not in (None, 1, 1.0, '1'):
            raise ValidationError({'detail': '该商品按单收费，不支持选择服务时长'})
        prepared = _force_base_quantity(validated_data, 1)
        return create_order_base(prepared, user, allow_existing_active=allow_existing_active)

    hours = requested_quantity
    if booked_hours_raw is not None:
        booked_hours = normalize_service_hours(booked_hours_raw)
        if booked_hours != requested_quantity:
            raise ValidationError({'detail': '商品数量与预订时长不一致，请重新下单'})
        hours = booked_hours

    prepared = _force_base_quantity(validated_data, hours)
    order = create_order_base(prepared, user, allow_existing_active=allow_existing_active)
    return _finalize_hourly_order(order, hours)


@transaction.atomic
def create_cart_orders(cart_item_ids, validated_data, user):
    """每个购物车条目创建一张订单；条目 quantity 作为该单服务时长。"""
    ordered_ids = []
    seen_ids = set()
    for raw_id in cart_item_ids or []:
        item_id = int(raw_id)
        if item_id not in seen_ids:
            ordered_ids.append(item_id)
            seen_ids.add(item_id)
    if not ordered_ids:
        raise ValidationError({'detail': '请选择需要结算的购物车商品'})
    if len(ordered_ids) > MAX_CART_ORDER_ITEMS:
        raise ValidationError({'detail': f'单次最多结算{MAX_CART_ORDER_ITEMS}项商品'})

    locked_items = list(
        CartItem.objects.select_for_update()
        .filter(id__in=ordered_ids, user=user)
        .select_related('package', 'spec')
    )
    item_map = {item.id: item for item in locked_items}
    if len(item_map) != len(ordered_ids):
        raise ValidationError({'detail': '部分购物车商品不存在、已变化或不属于当前账号'})

    ensure_no_active_orders(validated_data['boss_wechat'])
    orders = []
    for cart_item in [item_map[item_id] for item_id in ordered_ids]:
        hours = normalize_service_hours(cart_item.quantity)
        if not is_hourly_service(cart_item.package) and hours != 1:
            raise ValidationError({'detail': f'{cart_item.package.name}按单收费，数量必须为1'})

        order_payload = {
            'boss_wechat': validated_data['boss_wechat'],
            'game_id': validated_data.get('game_id'),
            'package_id': cart_item.package_id,
            'spec_id': cart_item.spec_id,
            'quantity': hours if is_hourly_service(cart_item.package) else 1,
            'required_players': cart_item.package.player_count,
            'addon_details': None,
            'designated_players': None,
            'boss_note': validated_data.get('boss_note'),
            'booked_hours': hours if is_hourly_service(cart_item.package) else 1,
        }
        orders.append(create_order(order_payload, user, allow_existing_active=True))

    CartItem.objects.filter(id__in=ordered_ids, user=user).delete()
    return orders
