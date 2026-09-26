from decimal import Decimal

from rest_framework.exceptions import ValidationError

from apps.catalog.models import PackageSpec
from apps.common.money import money
from apps.players.models import Player

from .models import Order


PRICING_LINE_SOURCE = 'designated_pricing'
PRICING_SUMMARY_SOURCE = 'designated_pricing_summary'


def _decimal(value):
    return Decimal(str(value or 0))


def _player_ids(raw_ids):
    result = []
    seen = set()
    for raw_id in raw_ids or []:
        try:
            player_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if player_id > 0 and player_id not in seen:
            seen.add(player_id)
            result.append(player_id)
    return result


def _pricing_base_rules(order):
    return [
        item for item in (order.designated_types or [])
        if item.get('source') not in {PRICING_LINE_SOURCE, PRICING_SUMMARY_SOURCE}
    ]


def calculate_designated_pricing(order, players=None):
    """
    订单规格只定义基础名额价格和公开抢单门槛；具体指定的每个名额，
    使用 max(基础单人价, 该陪玩最低指定计费类型单人价)。
    规格本身不会因为指定了更高类型陪玩而被替换。
    """
    if (
        order.order_type != Order.ORDER_TYPE_NORMAL
        or not order.package_id
        or not order.spec_id
    ):
        return None

    base_spec = (
        PackageSpec.objects
        .select_related('required_player_type', 'package')
        .filter(pk=order.spec_id, package_id=order.package_id, is_active=True)
        .first()
    )
    if not base_spec or not base_spec.required_player_type_id:
        return None

    required_players = max(1, int(base_spec.package.player_count or order.required_players or 1))
    base_total = money(base_spec.price)
    base_unit_raw = _decimal(base_total) / Decimal(required_players)
    base_type = base_spec.required_player_type
    base_priority = int(base_type.priority or 0)

    if players is None:
        player_ids = _player_ids(order.designated_players)
        players = list(
            Player.objects
            .filter(id__in=player_ids, status=Player.STATUS_APPROVED)
            .select_related('player_type', 'minimum_designated_player_type')
        )
        players_by_id = {player.id: player for player in players}
        players = [players_by_id[player_id] for player_id in player_ids if player_id in players_by_id]
    else:
        players = list(players)

    if len(players) > required_players:
        raise ValidationError({'designated_players': '指定陪玩人数不能超过订单人数'})

    tier_specs = list(
        PackageSpec.objects
        .select_related('required_player_type')
        .filter(
            package_id=order.package_id,
            is_active=True,
            required_player_type__isnull=False,
        )
        .order_by('sort_order', 'id')
    )
    specs_by_type = {}
    for spec in tier_specs:
        current = specs_by_type.get(spec.required_player_type_id)
        if current is None or money(spec.price) > money(current.price):
            specs_by_type[spec.required_player_type_id] = spec

    lines = []
    total_raw = base_unit_raw * Decimal(required_players - len(players))
    for player in players:
        actual_type = player.player_type
        actual_priority = int(actual_type.priority or 0)
        if actual_priority < base_priority:
            raise ValidationError({
                'designated_players': (
                    f'老板选择的是“{base_type.name}”及以上规格，'
                    f'指定陪玩“{player.name}”的实际类型为“{actual_type.name}”，不满足该订单要求。'
                )
            })

        billing_type = player.minimum_designated_player_type or actual_type
        billing_spec = specs_by_type.get(billing_type.id)
        if not billing_spec:
            raise ValidationError({
                'designated_players': (
                    f'商品“{base_spec.package.name}”未配置“{billing_type.name}”对应规格，'
                    f'无法计算指定陪玩“{player.name}”的价格。'
                )
            })

        billing_unit_raw = _decimal(money(billing_spec.price)) / Decimal(required_players)
        effective_unit_raw = max(base_unit_raw, billing_unit_raw)
        if billing_unit_raw > base_unit_raw:
            effective_type = billing_type
            effective_spec = billing_spec
        else:
            effective_type = base_type
            effective_spec = base_spec

        total_raw += effective_unit_raw
        lines.append({
            'source': PRICING_LINE_SOURCE,
            'player_id': player.id,
            'player_name': player.name,
            'actual_type_id': actual_type.id,
            'actual_type_name': actual_type.name,
            'minimum_billing_type_id': billing_type.id,
            'minimum_billing_type_name': billing_type.name,
            'effective_billing_type_id': effective_type.id,
            'effective_billing_type_name': effective_type.name,
            'effective_spec_id': effective_spec.id,
            'unit_price': float(money(effective_unit_raw)),
        })

    public_slots = required_players - len(players)
    total = money(total_raw)
    summary = {
        'source': PRICING_SUMMARY_SOURCE,
        'base_spec_id': base_spec.id,
        'base_spec_name': base_spec.display_name or base_spec.name,
        'base_type_id': base_type.id,
        'base_type_name': base_type.name,
        'base_unit_price': float(money(base_unit_raw)),
        'public_slots': public_slots,
        'total_per_hour': float(total),
    }
    return {
        'total': total,
        'designated_types': [*_pricing_base_rules(order), summary, *lines],
        'summary': summary,
        'lines': lines,
    }


def validate_designated_player_spec(order, players):
    """指定陪玩必须满足老板所选基础规格；价格按每人最低指定计费类型分别计算。"""
    if not players:
        return
    calculate_designated_pricing(order, players=players)
