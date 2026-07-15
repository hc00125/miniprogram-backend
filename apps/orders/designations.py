from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.catalog.models import PackageSpec
from apps.players.models import Player

from .models import Order, OrderDesignation, OrderPlayer, OrderStatusLog


DESIGNATION_TTL_MINUTES = 10
# P0 价格规则：具体指定本人不额外收费，技术/娱乐价格只由商品规格决定，禁止二次叠加。
DESIGNATION_EXTRA_AMOUNT = Decimal('0.00')
ACTIVE_PLAYER_ORDER_STATUSES = {
    Order.STATUS_PENDING_PAYMENT,
    Order.STATUS_READY_TO_START,
    Order.STATUS_IN_PROGRESS,
}


def normalize_designated_player_ids(raw_ids):
    result = []
    seen = set()
    for raw_id in raw_ids or []:
        try:
            player_id = int(raw_id)
        except (TypeError, ValueError):
            raise ValidationError({'designated_players': '指定陪玩参数不正确'})
        if player_id <= 0 or player_id in seen:
            continue
        seen.add(player_id)
        result.append(player_id)
    return result


def validate_designated_players(raw_ids, required_players):
    player_ids = normalize_designated_player_ids(raw_ids)
    if len(player_ids) > required_players:
        raise ValidationError({'designated_players': '指定陪玩人数不能超过订单人数'})
    if not player_ids:
        return []

    players = list(
        Player.objects
        .filter(id__in=player_ids, status=Player.STATUS_APPROVED)
        .select_related('player_type')
    )
    players_by_id = {player.id: player for player in players}
    missing = [player_id for player_id in player_ids if player_id not in players_by_id]
    if missing:
        raise ValidationError({'designated_players': '部分指定陪玩不存在或未通过审核'})

    # 方案二：一张订单可以指定多名陪玩，但阵容必须属于同一陪玩类型。
    player_type_ids = {player.player_type_id for player in players}
    if len(player_type_ids) > 1:
        type_names = sorted({player.player_type.name for player in players if player.player_type})
        raise ValidationError({
            'designated_players': f'当前仅支持指定同类型陪玩，已选择：{"、".join(type_names)}'
        })

    blocked = [player.name for player in players if not player.can_be_designated]
    if blocked:
        raise ValidationError({'designated_players': f"{'、'.join(blocked)}当前不接受指定"})

    busy_ids = set(
        OrderPlayer.objects
        .filter(
            player_id__in=player_ids,
            order__order_type=Order.ORDER_TYPE_NORMAL,
            order__status__in=ACTIVE_PLAYER_ORDER_STATUSES,
        )
        .values_list('player_id', flat=True)
    )
    if busy_ids:
        busy_names = [players_by_id[player_id].name for player_id in player_ids if player_id in busy_ids]
        raise ValidationError({'designated_players': f"{'、'.join(busy_names)}当前已有进行中的订单，暂不能指定"})

    return [players_by_id[player_id] for player_id in player_ids]


def validate_designated_player_spec(order, players):
    """指定具体陪玩时，陪玩等级必须达到商品规格的最低要求。"""
    if not players or not order.spec_id:
        return
    spec = (
        PackageSpec.objects
        .select_related('required_player_type')
        .filter(id=order.spec_id, package_id=order.package_id, is_active=True)
        .first()
    )
    if not spec or not spec.required_player_type_id:
        return

    required_priority = int(spec.required_player_type.priority or 0)
    incompatible = [
        player.name
        for player in players
        if int(player.player_type.priority or 0) < required_priority
    ]
    if incompatible:
        required_name = spec.required_player_type.name
        raise ValidationError({
            'designated_players': (
                f'所选规格“{spec.display_name or spec.name}”要求“{required_name}”及以上等级，'
                f'指定陪玩“{"、".join(incompatible)}”等级不足，请更换陪玩或改选较低等级规格。'
            )
        })


def create_designations(order, players):
    if not players:
        return []
    validate_designated_player_spec(order, players)
    expires_at = timezone.now() + timedelta(minutes=DESIGNATION_TTL_MINUTES)
    return [
        OrderDesignation.objects.create(
            order=order,
            player=player,
            status=OrderDesignation.STATUS_PENDING,
            extra_amount=DESIGNATION_EXTRA_AMOUNT,
            expires_at=expires_at,
        )
        for player in players
    ]


def sync_designated_players_snapshot(order):
    active_ids = list(
        order.designations
        .filter(status__in=[OrderDesignation.STATUS_PENDING, OrderDesignation.STATUS_ACCEPTED])
        .order_by('id')
        .values_list('player_id', flat=True)
    )
    order.designated_players = active_ids or None
    order.save(update_fields=['designated_players'])
    return active_ids


def expire_due_designations(order=None, now=None):
    now = now or timezone.now()
    queryset = OrderDesignation.objects.filter(
        status=OrderDesignation.STATUS_PENDING,
        expires_at__lte=now,
    )
    if order is not None:
        queryset = queryset.filter(order=order)
    affected_order_ids = list(queryset.values_list('order_id', flat=True).distinct())
    if not affected_order_ids:
        return 0
    updated = queryset.update(status=OrderDesignation.STATUS_EXPIRED, responded_at=now)
    for order_obj in Order.objects.filter(id__in=affected_order_ids):
        sync_designated_players_snapshot(order_obj)
        OrderStatusLog.objects.create(
            order=order_obj,
            from_status=order_obj.status,
            to_status=order_obj.status,
            reason='指定陪玩邀请超时，名额已转为公开抢单',
        )
    return updated


def pending_designation(order, player):
    return order.designations.filter(
        player=player,
        status=OrderDesignation.STATUS_PENDING,
        expires_at__gt=timezone.now(),
    ).first()


def pending_designation_count(order):
    return order.designations.filter(
        status=OrderDesignation.STATUS_PENDING,
        expires_at__gt=timezone.now(),
    ).count()


def remaining_public_slots(order):
    current_players = order.order_players.count()
    reserved_pending = pending_designation_count(order)
    return max(0, order.required_players - current_players - reserved_pending)


def finalize_lineup_if_full(order, operator=None, reason='接单人数已满，等待老板付款'):
    if order.order_players.count() < order.required_players:
        return order
    if order.status != Order.STATUS_WAITING:
        return order
    old_status = order.status
    order.status = Order.STATUS_PENDING_PAYMENT
    order.save(update_fields=['status'])
    OrderStatusLog.objects.create(
        order=order,
        from_status=old_status,
        to_status=order.status,
        operator=operator,
        reason=reason,
    )
    return order


@transaction.atomic
def accept_designation(order_no, player, operator=None):
    order = Order.objects.select_for_update().get(order_no=order_no)
    expire_due_designations(order=order)
    if order.order_type != Order.ORDER_TYPE_NORMAL or order.status != Order.STATUS_WAITING:
        raise ValidationError({'detail': '当前订单状态不能接受指定邀请'})
    if not player.can_accept_orders:
        raise ValidationError({'detail': '管理员已暂停您的接单权限'})
    if not player.can_be_designated:
        raise ValidationError({'detail': '管理员已暂停您的被指定权限'})

    designation = (
        OrderDesignation.objects
        .select_for_update()
        .filter(order=order, player=player)
        .first()
    )
    if not designation:
        raise ValidationError({'detail': '您没有该订单的指定邀请'})
    if designation.status == OrderDesignation.STATUS_ACCEPTED:
        return order
    if designation.status != OrderDesignation.STATUS_PENDING:
        raise ValidationError({'detail': f'该指定邀请已{designation.get_status_display()}'})
    if designation.expires_at <= timezone.now():
        designation.status = OrderDesignation.STATUS_EXPIRED
        designation.responded_at = timezone.now()
        designation.save(update_fields=['status', 'responded_at'])
        sync_designated_players_snapshot(order)
        raise ValidationError({'detail': '指定邀请已超时，名额已转为公开抢单'})
    if order.order_players.filter(player=player).exists():
        designation.status = OrderDesignation.STATUS_ACCEPTED
        designation.responded_at = timezone.now()
        designation.save(update_fields=['status', 'responded_at'])
        sync_designated_players_snapshot(order)
        return finalize_lineup_if_full(order, operator)
    if order.order_players.count() >= order.required_players:
        raise ValidationError({'detail': '订单人数已满'})

    OrderPlayer.objects.create(
        order=order,
        player=player,
        is_designated=True,
        designated_type_id=None,
    )
    player.total_orders = (player.total_orders or 0) + 1
    player.save(update_fields=['total_orders'])
    designation.status = OrderDesignation.STATUS_ACCEPTED
    designation.responded_at = timezone.now()
    designation.save(update_fields=['status', 'responded_at'])
    sync_designated_players_snapshot(order)
    OrderStatusLog.objects.create(
        order=order,
        from_status=order.status,
        to_status=order.status,
        operator=operator,
        reason=f'指定陪玩 {player.name} 已接受邀请',
    )
    return finalize_lineup_if_full(order, operator)


@transaction.atomic
def decline_designation(order_no, player, operator=None):
    order = Order.objects.select_for_update().get(order_no=order_no)
    designation = (
        OrderDesignation.objects
        .select_for_update()
        .filter(order=order, player=player)
        .first()
    )
    if not designation:
        raise ValidationError({'detail': '您没有该订单的指定邀请'})
    if designation.status != OrderDesignation.STATUS_PENDING:
        raise ValidationError({'detail': f'该指定邀请已{designation.get_status_display()}'})
    designation.status = OrderDesignation.STATUS_DECLINED
    designation.responded_at = timezone.now()
    designation.save(update_fields=['status', 'responded_at'])
    sync_designated_players_snapshot(order)
    OrderStatusLog.objects.create(
        order=order,
        from_status=order.status,
        to_status=order.status,
        operator=operator,
        reason=f'指定陪玩 {player.name} 已拒绝邀请，名额转为公开抢单',
    )
    return order


@transaction.atomic
def release_designation(order, designation_id, operator=None):
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.status != Order.STATUS_WAITING:
        raise ValidationError({'detail': '只有待接单订单可以取消指定'})
    designation = (
        OrderDesignation.objects
        .select_for_update()
        .select_related('player')
        .filter(order=order, id=designation_id)
        .first()
    )
    if not designation:
        raise ValidationError({'detail': '指定邀请不存在'})
    if designation.status != OrderDesignation.STATUS_PENDING:
        raise ValidationError({'detail': '只能取消尚未接受的指定邀请'})
    designation.status = OrderDesignation.STATUS_CANCELLED
    designation.responded_at = timezone.now()
    designation.save(update_fields=['status', 'responded_at'])
    sync_designated_players_snapshot(order)
    OrderStatusLog.objects.create(
        order=order,
        from_status=order.status,
        to_status=order.status,
        operator=operator,
        reason=f'老板取消指定 {designation.player.name}，名额转为公开抢单',
    )
    return designation
