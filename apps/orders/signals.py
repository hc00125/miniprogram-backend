from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.catalog.models import PackageSpec
from apps.common.money import money
from apps.players.models import Player

from .models import Order, OrderItem


def resolve_designated_pricing_spec(order):
    """指定陪玩等级高于所选规格时，使用其可对应的最高等级/最高价格规格。"""
    if (
        not order._state.adding
        or order.order_type != Order.ORDER_TYPE_NORMAL
        or not order.package_id
        or not order.spec_id
        or not order.designated_players
    ):
        return None

    selected_spec = (
        PackageSpec.objects
        .select_related('required_player_type')
        .filter(pk=order.spec_id, package_id=order.package_id, is_active=True)
        .first()
    )
    if not selected_spec or not selected_spec.required_player_type_id:
        return None

    player_ids = [int(value) for value in order.designated_players if str(value).isdigit()]
    players = list(
        Player.objects
        .filter(id__in=player_ids, status=Player.STATUS_APPROVED)
        .select_related('player_type')
    )
    if not players:
        return None

    highest_player_priority = max(int(player.player_type.priority or 0) for player in players)
    selected_priority = int(selected_spec.required_player_type.priority or 0)
    if highest_player_priority <= selected_priority:
        return None

    return (
        PackageSpec.objects
        .select_related('required_player_type')
        .filter(
            package_id=order.package_id,
            is_active=True,
            required_player_type__isnull=False,
            required_player_type__priority__gt=selected_priority,
            required_player_type__priority__lte=highest_player_priority,
        )
        .order_by('-required_player_type__priority', '-price', 'sort_order', 'id')
        .first()
    )


@receiver(pre_save, sender=Order, dispatch_uid='upgrade_designated_order_pricing_spec')
def upgrade_designated_order_pricing_spec(sender, instance, **kwargs):
    pricing_spec = resolve_designated_pricing_spec(instance)
    if not pricing_spec:
        return

    original_spec_name = instance.spec_name_snapshot or ''
    original_price = instance.spec_price_snapshot
    package_player_count = int(instance.package.player_count or 1)
    adjusted_price = money(pricing_spec.price)

    instance.spec_id = pricing_spec.id
    instance.spec_name_snapshot = pricing_spec.display_name or pricing_spec.name
    instance.spec_price_snapshot = float(adjusted_price)
    instance.required_players = package_player_count
    instance.designated_types = [{
        'type_id': pricing_spec.required_player_type_id,
        'count': package_player_count,
        'source': 'spec',
    }]
    instance.total_price_per_hour = float(adjusted_price)
    instance.total_amount = float(adjusted_price)

    adjustment_note = (
        f'指定陪玩等级计价：由“{original_spec_name or "原规格"}”'
        f'（¥{money(original_price or 0)}）自动调整为“{pricing_spec.display_name or pricing_spec.name}”'
        f'（¥{adjusted_price}）。'
    )
    current_note = str(instance.boss_note or '').strip()
    if adjustment_note not in current_note:
        instance.boss_note = f'{current_note}\n{adjustment_note}'.strip()


@receiver(post_save, sender=OrderItem, dispatch_uid='sync_designated_pricing_order_item')
def sync_designated_pricing_order_item(sender, instance, created, **kwargs):
    if not created or not instance.order_id:
        return

    order = (
        Order.objects
        .select_related('package')
        .filter(pk=instance.order_id)
        .first()
    )
    if not order or not order.spec_id or instance.spec_id == order.spec_id:
        return

    pricing_spec = PackageSpec.objects.filter(pk=order.spec_id, package_id=order.package_id).first()
    if not pricing_spec:
        return

    price = money(pricing_spec.price)
    OrderItem.objects.filter(pk=instance.pk).update(
        spec=pricing_spec,
        spec_name=pricing_spec.name,
        spec_display_name=pricing_spec.display_name or pricing_spec.name,
        unit_price=float(price),
        quantity=1,
        amount=float(price),
    )
