"""Server-side pricing for the static designated-play composition flow.

This module is deliberately the only order-layer entry point that turns a
selected set of concrete players into a price.  Concrete identities are used
only to validate eligibility and to choose an allowed billing *type*; the
resulting price and virtual product are resolved from the static catalog
``CompositionSku`` keyed by type counts.
"""

from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from rest_framework.exceptions import ValidationError

from apps.catalog.composition import (
    calculate_static_composition,
    canonicalize_designated_type_signature,
)
from apps.catalog.models import CompositionSku, PackageSpec, PlayerOffer
from apps.players.models import Player

from .models import Order, OrderPricingLine


MONEY_QUANTUM = Decimal('0.01')
COMPOSITION_SUMMARY_SOURCE = 'composition_pricing_summary'
COMPOSITION_LINE_SOURCE = 'composition_pricing'


def _money(value):
    return Decimal(str(value or 0)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _as_positive_int(value, field_name):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError({field_name: '参数必须是有效整数'}) from exc
    if result <= 0:
        raise ValidationError({field_name: '参数必须大于 0'})
    return result


def normalize_designated_player_ids(raw_ids):
    """Keep user selection order for display while rejecting duplicates."""
    result = []
    seen = set()
    for raw_id in raw_ids or []:
        player_id = _as_positive_int(raw_id, 'designated_player_ids')
        if player_id in seen:
            raise ValidationError({'designated_player_ids': '不能重复指定同一位陪玩师'})
        seen.add(player_id)
        result.append(player_id)
    return result


def _base_context(base_spec):
    if not base_spec or not base_spec.pk:
        raise ValidationError({'base_spec_id': '基础规格不存在'})
    if not base_spec.is_active or not base_spec.package.is_active:
        raise ValidationError({'base_spec_id': '基础商品或规格已下架'})
    if not base_spec.required_player_type_id:
        raise ValidationError({'base_spec_id': '基础规格未配置陪玩类型，不能用于指定陪玩'})

    package = base_spec.package
    family = getattr(package, 'package_family', None)
    if not family or not family.is_active:
        raise ValidationError({'base_spec_id': '该商品未配置启用的装备商品族，暂不能指定陪玩'})

    required_players = int(package.player_count or 0)
    if required_players not in {1, 2, 3}:
        raise ValidationError({'base_spec_id': '指定陪玩组合当前仅支持 1、2 或 3 人套餐'})

    # A family can contain only one sellable package for each headcount.  Do not
    # silently quote a sibling package when the user selected a different one.
    family_package = family.package_for_player_count(required_players, active_only=True)
    if not family_package or family_package.id != package.id:
        raise ValidationError({'base_spec_id': '基础套餐不是该装备商品族当前有效的人数套餐'})
    return package, family, base_spec.required_player_type, required_players


def _resolve_players(player_ids, *, family, base_type, required_players):
    if len(player_ids) > required_players:
        raise ValidationError({'designated_player_ids': '指定人数不能超过套餐总人数'})
    if not player_ids:
        return []

    players = list(
        Player.objects.filter(id__in=player_ids, status=Player.STATUS_APPROVED)
        .select_related('player_type', 'minimum_designated_player_type')
    )
    by_id = {player.id: player for player in players}
    missing = [player_id for player_id in player_ids if player_id not in by_id]
    if missing:
        raise ValidationError({'designated_player_ids': '部分指定陪玩师不存在或未通过审核'})

    # The offer assignment is availability configuration only; no per-player
    # price is read from it.  This keeps the CompositionSku finite and static.
    offered_ids = set(
        PlayerOffer.objects.active()
        .filter(package_family=family, player_id__in=player_ids)
        .values_list('player_id', flat=True)
    )
    unavailable = [by_id[player_id].name for player_id in player_ids if player_id not in offered_ids]
    if unavailable:
        raise ValidationError({
            'designated_player_ids': f"{'、'.join(unavailable)} 当前未上架该装备套餐",
        })

    base_priority = int(base_type.priority or 0)
    result = []
    for player_id in player_ids:
        player = by_id[player_id]
        if not player.can_be_designated:
            raise ValidationError({'designated_player_ids': f'陪玩师“{player.name}”当前不接受指定'})
        actual_type = player.player_type
        if not actual_type or int(actual_type.priority or 0) < base_priority:
            raise ValidationError({
                'designated_player_ids': (
                    f'陪玩师“{player.name}”的等级不满足基础类型“{base_type.name}”'
                ),
            })

        minimum_type = player.minimum_designated_player_type or actual_type
        # The minimum designated billing type is allowed to be below the
        # player's real tier, but never below the selected base package tier.
        billing_type = (
            minimum_type
            if int(minimum_type.priority or 0) >= base_priority
            else base_type
        )
        result.append({
            'player': player,
            'actual_type': actual_type,
            'minimum_billing_type': minimum_type,
            'billing_type': billing_type,
        })
    return result


def _composition_sku(*, family, base_type, required_players, signature, calculated):
    sku = CompositionSku.resolve_for(
        package_family=family,
        base_player_type=base_type,
        required_players=required_players,
        designated_type_signature=signature,
        active_only=True,
    )
    if not sku:
        raise ValidationError({
            'detail': '当前指定组合尚未配置静态结算商品，请联系运营补齐组合 SKU 后重试',
        })
    if not sku.virtual_package_spec_id:
        raise ValidationError({'detail': '当前组合 SKU 未绑定内部虚拟支付规格'})
    if not sku.virtual_package_spec.is_active or not sku.virtual_package_spec.package.is_active:
        raise ValidationError({'detail': '当前组合 SKU 的虚拟支付规格已下架'})

    calculated_total = _money(calculated['total_price_per_hour'])
    if _money(sku.total_price_per_hour) != calculated_total:
        raise ValidationError({
            'detail': '组合 SKU 静态价格与装备套餐规格不一致，请联系运营修复配置',
        })
    if _money(sku.virtual_package_spec.price) != calculated_total:
        raise ValidationError({
            'detail': '组合 SKU 的虚拟支付规格价格与静态组合价格不一致',
        })
    return sku


def validate_composition_virtual_binding(sku):
    """Require the one static virtual item before an invitation is created."""
    binding = sku.active_virtual_binding()
    if not binding:
        raise ValidationError({
            'detail': '当前组合 SKU 尚未绑定价格一致的微信虚拟商品，暂不能提交订单',
        })
    return binding


def quote_composition(*, base_spec, designated_player_ids=None, booked_hours=None, require_virtual_binding=False):
    """Resolve an immutable composition quote from a base package and players.

    ``booked_hours`` is intentionally validated as a positive integer because
    virtual payment uses the duration as its one product's ``buyQuantity``.
    """
    package, family, base_type, required_players = _base_context(base_spec)
    player_ids = normalize_designated_player_ids(designated_player_ids)
    player_rows = _resolve_players(
        player_ids,
        family=family,
        base_type=base_type,
        required_players=required_players,
    )
    signature = canonicalize_designated_type_signature([
        {'player_type_id': row['billing_type'].id, 'count': 1}
        for row in player_rows
    ])
    calculated = calculate_static_composition(
        package_family=family,
        base_player_type=base_type,
        required_players=required_players,
        designated_type_signature=signature,
        active_only=True,
    )
    sku = _composition_sku(
        family=family,
        base_type=base_type,
        required_players=required_players,
        signature=signature,
        calculated=calculated,
    )
    # Quotes remain viewable while operations are completing configuration, but
    # expose a binding when one exists so the client can show payment readiness.
    binding = sku.active_virtual_binding()
    if require_virtual_binding and not binding:
        binding = validate_composition_virtual_binding(sku)

    duration = None
    if booked_hours is not None:
        duration = _as_positive_int(booked_hours, 'booked_hours')

    by_type = defaultdict(list)
    for row in player_rows:
        by_type[row['billing_type'].id].append(row)

    line_rows = []
    for index, line in enumerate(calculated['lines']):
        kind = line['kind']
        type_players = by_type.get(line['player_type_id'], []) if kind == 'designated' else []
        line_rows.append({
            **line,
            'sort_order': index,
            'player_ids': [row['player'].id for row in type_players],
            'player_names': [row['player'].name for row in type_players],
        })

    total_per_hour = _money(calculated['total_price_per_hour'])
    return {
        'base_package': package,
        'base_spec': base_spec,
        'package_family': family,
        'base_player_type': base_type,
        'required_players': required_players,
        'designated_player_rows': player_rows,
        'designated_player_ids': player_ids,
        'designated_type_signature': signature,
        'composition_sku': sku,
        'virtual_binding': binding,
        'virtual_package_spec': sku.virtual_package_spec,
        'composition_key': calculated['composition_key'],
        'public_players': calculated['public_players'],
        'total_price_per_hour': total_per_hour,
        'booked_hours': duration,
        'total_amount': _money(total_per_hour * duration) if duration is not None else None,
        'lines': line_rows,
    }


def quote_composition_for_order(order, *, require_virtual_binding=False):
    if order.pricing_mode != Order.PRICING_MODE_COMPOSITION:
        raise ValidationError({'detail': '该订单不是静态组合定价订单'})
    base_spec = (
        PackageSpec.objects.select_related('package__package_family', 'required_player_type')
        .filter(pk=order.spec_id, package_id=order.package_id)
        .first()
    )
    if not base_spec:
        raise ValidationError({'detail': '订单基础规格不存在，无法重新报价'})
    return quote_composition(
        base_spec=base_spec,
        designated_player_ids=order.designated_players or [],
        booked_hours=order.booked_hours,
        require_virtual_binding=require_virtual_binding,
    )


def apply_quote_to_order(order, quote):
    """Update in-memory order fields from a quote without triggering legacy pricing."""
    sku = quote['composition_sku']
    total = _money(quote['total_price_per_hour'])
    order.pricing_mode = Order.PRICING_MODE_COMPOSITION
    order.composition_sku_id = sku.id
    order.composition_key = quote['composition_key']
    order.composition_virtual_spec_id = sku.virtual_package_spec_id
    order.composition_price_per_hour = total
    order.composition_pricing_error = ''
    order.total_price_per_hour = float(total)
    order.total_amount = float(quote['total_amount'] if quote['total_amount'] is not None else total)
    order.required_players = quote['required_players']
    order.designated_types = [
        {
            'source': 'spec',
            'type_id': quote['base_player_type'].id,
            'count': quote['required_players'],
        },
        {
            'source': COMPOSITION_SUMMARY_SOURCE,
            'composition_sku_id': sku.id,
            'composition_key': quote['composition_key'],
            'designated_type_signature': quote['designated_type_signature'],
            'public_players': quote['public_players'],
            'total_per_hour': float(total),
        },
    ]
    return order


def _line_snapshot_rows(quote):
    """Expand each static type line into auditable order price-line rows."""
    rows = []
    for line in quote['lines']:
        unit_price = _money(line['unit_price_per_hour'])
        if line['kind'] == 'designated':
            # Static calculator aggregates same billing types, while the order
            # audit trail intentionally retains the selected players one each.
            for player_id, player_name in zip(line['player_ids'], line['player_names']):
                rows.append({
                    'source': OrderPricingLine.SOURCE_DESIGNATED,
                    'player_id_snapshot': player_id,
                    'player_name_snapshot': player_name,
                    'billing_player_type_id': line['player_type_id'],
                    'billing_player_type_name': line['player_type_name'],
                    'package_id_snapshot': line['package_id'],
                    'package_name_snapshot': line['package_name'],
                    'spec_id_snapshot': line['package_spec_id'],
                    'spec_name_snapshot': line['package_spec_name'],
                    'quantity': 1,
                    'unit_price': unit_price,
                    'amount': unit_price,
                    'sort_order': len(rows),
                })
        else:
            rows.append({
                'source': OrderPricingLine.SOURCE_PUBLIC,
                'player_id_snapshot': None,
                'player_name_snapshot': '',
                'billing_player_type_id': line['player_type_id'],
                'billing_player_type_name': line['player_type_name'],
                'package_id_snapshot': line['package_id'],
                'package_name_snapshot': line['package_name'],
                'spec_id_snapshot': line['package_spec_id'],
                'spec_name_snapshot': line['package_spec_name'],
                'quantity': line['player_count'],
                'unit_price': unit_price,
                'amount': _money(line['amount_per_hour']),
                'sort_order': len(rows),
            })
    return rows


def replace_order_pricing_lines(order, quote):
    if not order.pk:
        raise ValueError('订单必须先保存后才能写入价格明细')
    OrderPricingLine.objects.filter(order=order).delete()
    OrderPricingLine.objects.bulk_create([
        OrderPricingLine(order=order, **row)
        for row in _line_snapshot_rows(quote)
    ])


def reprice_composition_order(order, *, require_virtual_binding=False):
    """Re-match a static SKU after a designation is released/expired/declined."""
    quote = quote_composition_for_order(order, require_virtual_binding=require_virtual_binding)
    apply_quote_to_order(order, quote)
    Order.objects.filter(pk=order.pk, paid=False).update(
        composition_sku_id=order.composition_sku_id,
        composition_key=order.composition_key,
        composition_virtual_spec_id=order.composition_virtual_spec_id,
        composition_price_per_hour=order.composition_price_per_hour,
        composition_pricing_error='',
        total_price_per_hour=order.total_price_per_hour,
        total_amount=order.total_amount,
        required_players=order.required_players,
        designated_types=order.designated_types,
    )
    replace_order_pricing_lines(order, quote)
    return quote


def mark_composition_pricing_unavailable(order, error):
    """Leave a released/expired slot public even when its fallback SKU is absent.

    A configuration gap must never trap a player in an invitation.  We preserve
    the old monetary snapshot only for audit, remove the payment SKU mapping,
    and leave an explicit marker that makes virtual payment fail safely until a
    later player-change/config repair can re-match a valid static SKU.
    """
    message = str(error)
    if hasattr(error, 'detail'):
        message = str(error.detail)
    message = message[:300]
    current_rules = [
        item for item in (order.designated_types or [])
        if item.get('source') not in {COMPOSITION_SUMMARY_SOURCE, COMPOSITION_LINE_SOURCE}
    ]
    if not any(item.get('source') == 'spec' for item in current_rules):
        try:
            base_spec = PackageSpec.objects.select_related('required_player_type').get(pk=order.spec_id)
        except PackageSpec.DoesNotExist:
            base_spec = None
        if base_spec and base_spec.required_player_type_id:
            current_rules.append({
                'source': 'spec',
                'type_id': base_spec.required_player_type_id,
                'count': int(order.required_players or base_spec.package.player_count or 1),
            })
    current_rules.append({
        'source': COMPOSITION_SUMMARY_SOURCE,
        'configuration_error': message,
        'payment_blocked': True,
    })
    Order.objects.filter(pk=order.pk, paid=False).update(
        composition_sku_id=None,
        composition_key='',
        composition_virtual_spec_id=None,
        composition_pricing_error=message,
        designated_types=current_rules,
    )
    # No line should claim to be a current price after one of its designated
    # names has been released.  The valid prior payment snapshot remains on the
    # order itself for support/audit, but the order is deliberately unpayable.
    OrderPricingLine.objects.filter(order=order).delete()
    order.composition_sku_id = None
    order.composition_key = ''
    order.composition_virtual_spec_id = None
    order.composition_pricing_error = message
    order.designated_types = current_rules
    return message


def serialize_composition_quote(quote):
    """JSON-safe response used by draft quote and submit endpoints."""
    sku = quote['composition_sku']
    binding = quote.get('virtual_binding')
    return {
        'composition_sku': {
            'id': sku.id,
            'composition_key': quote['composition_key'],
            'virtual_package_spec_id': sku.virtual_package_spec_id,
            'virtual_product_id': binding.product_id if binding else None,
            'virtual_goods_price_fen': binding.goods_price_fen if binding else None,
        },
        'base_package_id': quote['base_package'].id,
        'base_spec_id': quote['base_spec'].id,
        'package_family_id': quote['package_family'].id,
        'package_family_code': quote['package_family'].code,
        'base_player_type_id': quote['base_player_type'].id,
        'base_player_type_name': quote['base_player_type'].name,
        'required_players': quote['required_players'],
        'public_players': quote['public_players'],
        'designated_player_ids': quote['designated_player_ids'],
        'designated_type_signature': quote['designated_type_signature'],
        'booked_hours': quote['booked_hours'],
        'total_price_per_hour': float(quote['total_price_per_hour']),
        'total_amount': float(quote['total_amount']) if quote['total_amount'] is not None else None,
        'lines': [
            {
                'source': line['kind'],
                'player_type_id': line['player_type_id'],
                'player_type_name': line['player_type_name'],
                'player_count': line['player_count'],
                'player_ids': line['player_ids'],
                'player_names': line['player_names'],
                # Singular aliases make the UI resilient when the static
                # calculator aggregates several players with one billing tier.
                'player_name': '、'.join(line['player_names']),
                'package_id': line['package_id'],
                'package_name': line['package_name'],
                'spec_id': line['package_spec_id'],
                'spec_name': line['package_spec_name'],
                'unit_price_per_hour': float(_money(line['unit_price_per_hour'])),
                'amount_per_hour': float(_money(line['amount_per_hour'])),
                'unit_price': float(_money(line['unit_price_per_hour'])),
                'amount': float(_money(line['amount_per_hour'])),
                'title': (
                    f"{'、'.join(line['player_names'])} · {line['player_type_name']}"
                    if line['kind'] == 'designated' and line['player_names']
                    else f"公开{line['player_count']}人 · {line['player_type_name']}"
                ),
            }
            for line in quote['lines']
        ],
    }
