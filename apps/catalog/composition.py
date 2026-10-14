"""Static pricing helpers for designated-play composition SKUs.

The functions in this module deliberately use the catalog's static package/spec
matrix only.  They do not inspect orders, invitations, or a player's personal
price, so both quote creation and payment validation can use the same result.
"""

from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError


MONEY_QUANTUM = Decimal('0.01')


def _pk(value, field_name):
    value = getattr(value, 'pk', value)
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f'{field_name} 必须是有效 ID。') from exc
    if value <= 0:
        raise ValidationError(f'{field_name} 必须是有效 ID。')
    return value


def _money(value):
    return Decimal(str(value or 0)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def canonicalize_designated_type_signature(signature):
    """Normalize a type-count signature into a stable, JSON-safe list.

    The only accepted public wire shape is ``[{"player_type_id": 3,
    "count": 1}]``. Repeated types are merged and entries are sorted by type
    ID so the resulting composition key is independent of selection order.
    """
    if signature in (None, ''):
        return []
    if not isinstance(signature, (list, tuple)):
        raise ValidationError('指定计费类型签名必须是数组。')

    counts = {}
    for item in signature:
        if not isinstance(item, dict):
            raise ValidationError('指定计费类型签名的每一项必须包含 player_type_id 和 count。')
        try:
            player_type_id = int(item.get('player_type_id'))
            count = int(item.get('count'))
        except (TypeError, ValueError) as exc:
            raise ValidationError('指定计费类型签名中的 player_type_id 和 count 必须是整数。') from exc
        if player_type_id <= 0 or count <= 0:
            raise ValidationError('指定计费类型签名中的 player_type_id 和 count 必须大于 0。')
        counts[player_type_id] = counts.get(player_type_id, 0) + count

    return [
        {'player_type_id': player_type_id, 'count': counts[player_type_id]}
        for player_type_id in sorted(counts)
    ]


def build_composition_key(*, package_family, base_player_type, required_players, designated_type_signature):
    """Return the persisted key for one static composition SKU."""
    family_code = getattr(package_family, 'code', None)
    if not family_code:
        # Callers in order/API code may only have the family ID.  Resolve it
        # instead of making a second, ID-shaped key that could never match the
        # stable code-based key stored on CompositionSku.
        family_code = _resolve_family(package_family).code
    base_player_type_id = _pk(base_player_type, 'base_player_type')
    try:
        required_players = int(required_players)
    except (TypeError, ValueError) as exc:
        raise ValidationError('required_players 必须是整数。') from exc
    signature = canonicalize_designated_type_signature(designated_type_signature)
    signature_text = ','.join(
        f'{item["player_type_id"]}x{item["count"]}'
        for item in signature
    ) or 'none'
    return (
        f'family:{family_code}|base:{base_player_type_id}|'
        f'players:{required_players}|designated:{signature_text}'
    )


def _resolve_family(package_family):
    if getattr(package_family, 'pk', None):
        return package_family
    from .models import PackageFamily

    try:
        return PackageFamily.objects.get(pk=_pk(package_family, 'package_family'))
    except PackageFamily.DoesNotExist as exc:
        raise ValidationError('装备商品族不存在。') from exc


def _resolve_player_type(player_type, *, field_name):
    if getattr(player_type, 'pk', None):
        return player_type
    from .models import PlayerType

    try:
        return PlayerType.objects.get(pk=_pk(player_type, field_name))
    except PlayerType.DoesNotExist as exc:
        raise ValidationError(f'{field_name} 不存在。') from exc


def _tier_spec(package_family, player_type, player_count, *, active_only):
    package = package_family.package_for_player_count(player_count, active_only=active_only)
    if not package:
        raise ValidationError(
            f'装备商品族“{package_family.name}”缺少 {player_count} 人套餐。'
        )

    specs = package.specs.filter(required_player_type=player_type)
    if active_only:
        specs = specs.filter(is_active=True)
    spec = specs.order_by('sort_order', 'id').first()
    if not spec:
        state = '启用的' if active_only else ''
        raise ValidationError(
            f'套餐“{package.name}”缺少“{player_type.name}”的{state}计价规格。'
        )
    return package, spec


def calculate_static_composition(
    *,
    package_family,
    base_player_type,
    required_players,
    designated_type_signature,
    active_only=True,
):
    """Calculate one composition using static 1-person and remainder SKUs.

    Returned lines are intentionally stable and include the source package/spec
    IDs, counts, unit price, and extended amount so callers can persist an
    order-price snapshot without reimplementing catalog selection rules.
    """
    family = _resolve_family(package_family)
    base_type = _resolve_player_type(base_player_type, field_name='base_player_type')
    try:
        required_players = int(required_players)
    except (TypeError, ValueError) as exc:
        raise ValidationError('required_players 必须是整数。') from exc
    if required_players not in {1, 2, 3}:
        raise ValidationError('组合结算仅支持 1、2 或 3 人。')

    signature = canonicalize_designated_type_signature(designated_type_signature)
    designated_count = sum(item['count'] for item in signature)
    if designated_count > required_players:
        raise ValidationError('指定陪玩人数不能超过总人数。')

    from .models import PlayerType

    type_ids = [item['player_type_id'] for item in signature]
    types_by_id = PlayerType.objects.in_bulk(type_ids)
    missing_ids = [player_type_id for player_type_id in type_ids if player_type_id not in types_by_id]
    if missing_ids:
        raise ValidationError(f'指定计费类型不存在：{", ".join(map(str, missing_ids))}。')

    base_priority = int(base_type.priority or 0)
    lower_types = [
        player_type.name
        for player_type in types_by_id.values()
        if int(player_type.priority or 0) < base_priority
    ]
    if lower_types:
        raise ValidationError(
            f'指定计费类型不能低于基础陪玩类型“{base_type.name}”：{ "、".join(lower_types) }。'
        )

    lines = []
    total = Decimal('0.00')
    for item in signature:
        player_type = types_by_id[item['player_type_id']]
        package, spec = _tier_spec(family, player_type, 1, active_only=active_only)
        unit_price = _money(spec.price)
        amount = _money(unit_price * item['count'])
        total += amount
        lines.append({
            'kind': 'designated',
            'player_type_id': player_type.id,
            'player_type_name': player_type.name,
            'player_type_priority': int(player_type.priority or 0),
            'player_count': item['count'],
            'package_id': package.id,
            'package_name': package.name,
            'package_player_count': package.player_count,
            'package_spec_id': spec.id,
            'package_spec_name': spec.display_name or spec.name,
            'unit_price_per_hour': unit_price,
            'amount_per_hour': amount,
        })

    public_players = required_players - designated_count
    if public_players:
        package, spec = _tier_spec(family, base_type, public_players, active_only=active_only)
        unit_price = _money(spec.price)
        total += unit_price
        lines.append({
            'kind': 'public',
            'player_type_id': base_type.id,
            'player_type_name': base_type.name,
            'player_type_priority': int(base_type.priority or 0),
            'player_count': public_players,
            'package_id': package.id,
            'package_name': package.name,
            'package_player_count': package.player_count,
            'package_spec_id': spec.id,
            'package_spec_name': spec.display_name or spec.name,
            'unit_price_per_hour': unit_price,
            'amount_per_hour': unit_price,
        })

    total = _money(total)
    return {
        'composition_key': build_composition_key(
            package_family=family,
            base_player_type=base_type,
            required_players=required_players,
            designated_type_signature=signature,
        ),
        'package_family_id': family.id,
        'package_family_code': family.code,
        'base_player_type_id': base_type.id,
        'base_player_type_name': base_type.name,
        'required_players': required_players,
        'designated_type_signature': signature,
        'designated_players': designated_count,
        'public_players': public_players,
        'total_price_per_hour': total,
        'lines': lines,
    }
