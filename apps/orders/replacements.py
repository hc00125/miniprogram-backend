from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.orders.models import Order, OrderDesignation, OrderPlayer, OrderStatusLog
from apps.players.models import Player

from .cancellation_models import OrderReplacementState, PlayerCancellationRecord
from .discipline import ensure_player_can_accept


def replacement_payload(order):
    state = OrderReplacementState.objects.filter(order=order).first()
    if not state or state.status == OrderReplacementState.STATUS_RESOLVED:
        return {'active': False, 'status': state.status if state else ''}
    return {
        'active': True,
        'mode': state.mode,
        'mode_text': state.get_mode_display(),
        'status': state.status,
        'status_text': state.get_status_display(),
        'missing_slots': state.missing_slots,
        'resume_status': state.resume_status,
        'remaining_minutes': state.remaining_minutes,
        'cancelled_player_name': state.cancelled_player_name,
        'required_player_type_id': state.required_player_type_id,
        'required_player_type_name': state.required_player_type_name,
        'can_reassign': state.mode == OrderReplacementState.MODE_TARGETED and state.status == OrderReplacementState.STATUS_OPEN,
        'can_publish_public': state.status == OrderReplacementState.STATUS_OPEN,
        'can_request_cancel': state.status == OrderReplacementState.STATUS_OPEN,
        'created_at': state.created_at,
        'updated_at': state.updated_at,
    }


def ensure_order_can_start(order):
    state = OrderReplacementState.objects.filter(order=order, status=OrderReplacementState.STATUS_OPEN).first()
    if state:
        detail = '当前有陪玩退出，需等待老板重新指定' if state.mode == OrderReplacementState.MODE_TARGETED else '当前正在补位，人数补齐后才能开打'
        raise ValidationError({'detail': detail})


def finalize_replacement_if_full(order):
    state = OrderReplacementState.objects.filter(order=order, status=OrderReplacementState.STATUS_OPEN).first()
    if not state or order.order_players.count() < order.required_players:
        return order
    if state.resume_status == Order.STATUS_IN_PROGRESS:
        order.status = Order.STATUS_IN_PROGRESS
        order.save(update_fields=['status'])
    elif order.paid:
        order.status = Order.STATUS_READY_TO_START
        order.save(update_fields=['status'])
    state.status = OrderReplacementState.STATUS_RESOLVED
    state.missing_slots = 0
    state.resolved_at = timezone.now()
    state.save(update_fields=['status', 'missing_slots', 'resolved_at', 'updated_at'])
    return order


@transaction.atomic
def grab_order_with_replacement(original_grab, order_no, player, operator=None):
    ensure_player_can_accept(player)
    order = Order.objects.select_for_update().filter(order_no=order_no).first()
    if not order:
        raise Order.DoesNotExist
    state = OrderReplacementState.objects.select_for_update().filter(
        order=order,
        mode=OrderReplacementState.MODE_PUBLIC,
        status=OrderReplacementState.STATUS_OPEN,
    ).first()
    if not state or order.status == Order.STATUS_WAITING:
        return finalize_replacement_if_full(original_grab(order_no, player, operator))

    if order.order_players.filter(player=player).exists():
        raise ValidationError({'detail': '您已经在这个订单中'})
    if PlayerCancellationRecord.objects.filter(order=order, player=player).exists():
        raise ValidationError({'detail': '您已取消过该订单，不能再次抢回'})
    if order.order_players.count() >= order.required_players:
        raise ValidationError({'detail': '补位名额已满'})

    OrderPlayer.objects.create(order=order, player=player, is_designated=False)
    player.total_orders = int(player.total_orders or 0) + 1
    player.save(update_fields=['total_orders'])
    state.missing_slots = max(0, order.required_players - order.order_players.count())
    state.save(update_fields=['missing_slots', 'updated_at'])
    return finalize_replacement_if_full(order)


@transaction.atomic
def publish_replacement_public(order, operator=None):
    state = OrderReplacementState.objects.select_for_update().filter(order=order).first()
    if not state or state.status != OrderReplacementState.STATUS_OPEN:
        raise ValidationError({'detail': '当前没有待处理的补位'})
    now = timezone.now()
    OrderDesignation.objects.filter(order=order, status=OrderDesignation.STATUS_PENDING).update(
        status=OrderDesignation.STATUS_CANCELLED,
        responded_at=now,
    )
    old_status = order.status
    order.fulfillment_mode = Order.FULFILLMENT_MODE_PUBLIC
    order.target_player = None
    order.target_player_name_snapshot = ''
    order.designated_players = None
    if order.status != Order.STATUS_IN_PROGRESS:
        order.status = Order.STATUS_WAITING
    order.save(update_fields=['fulfillment_mode', 'target_player', 'target_player_name_snapshot', 'designated_players', 'status'])
    state.mode = OrderReplacementState.MODE_PUBLIC
    state.save(update_fields=['mode', 'updated_at'])
    OrderStatusLog.objects.create(order=order, from_status=old_status, to_status=order.status, operator=operator, reason='老板已将退出名额转为公开补位')
    return state


@transaction.atomic
def reassign_replacement(order, player_id, operator=None):
    state = OrderReplacementState.objects.select_for_update().filter(order=order).first()
    if not state or state.status != OrderReplacementState.STATUS_OPEN:
        raise ValidationError({'detail': '当前没有待重新指定的名额'})
    player = Player.objects.select_related('player_type').filter(id=player_id, status=Player.STATUS_APPROVED).first()
    if not player:
        raise ValidationError({'detail': '陪玩师不存在或未通过审核'})
    if not player.can_accept_orders or not player.can_be_designated:
        raise ValidationError({'detail': '该陪玩师当前不接受指定'})
    ensure_player_can_accept(player)
    if state.required_player_type_id and player.player_type_id != state.required_player_type_id:
        raise ValidationError({'detail': f'请重新选择“{state.required_player_type_name}”类型的陪玩师，避免改变原订单计价'})
    if OrderPlayer.objects.filter(
        player=player,
        order__order_type=Order.ORDER_TYPE_NORMAL,
        order__status__in=[Order.STATUS_PENDING_PAYMENT, Order.STATUS_READY_TO_START, Order.STATUS_IN_PROGRESS],
    ).exists():
        raise ValidationError({'detail': '该陪玩师当前已有进行中的订单'})

    now = timezone.now()
    OrderDesignation.objects.filter(order=order, status=OrderDesignation.STATUS_PENDING).update(
        status=OrderDesignation.STATUS_CANCELLED,
        responded_at=now,
    )
    designation = OrderDesignation.objects.filter(order=order, player=player).first()
    if designation:
        designation.status = OrderDesignation.STATUS_PENDING
        designation.responded_at = None
        designation.expires_at = now + timedelta(minutes=10)
        designation.save(update_fields=['status', 'responded_at', 'expires_at'])
    else:
        designation = OrderDesignation.objects.create(
            order=order,
            player=player,
            status=OrderDesignation.STATUS_PENDING,
            extra_amount=Decimal('0.00'),
            expires_at=now + timedelta(minutes=10),
        )

    old_status = order.status
    order.fulfillment_mode = Order.FULFILLMENT_MODE_TARGETED
    order.target_player = player
    order.target_player_name_snapshot = player.name
    order.designated_players = [player.id]
    if order.status != Order.STATUS_IN_PROGRESS:
        order.status = Order.STATUS_WAITING
    order.save(update_fields=['fulfillment_mode', 'target_player', 'target_player_name_snapshot', 'designated_players', 'status'])
    OrderStatusLog.objects.create(order=order, from_status=old_status, to_status=order.status, operator=operator, reason=f'老板重新指定陪玩师 {player.name}，等待对方接受')
    return designation


@transaction.atomic
def request_cancel_remaining(order, operator=None):
    state = OrderReplacementState.objects.select_for_update().filter(order=order).first()
    if not state or state.status != OrderReplacementState.STATUS_OPEN:
        raise ValidationError({'detail': '当前没有可申请取消的剩余服务'})
    state.status = OrderReplacementState.STATUS_CANCEL_REQUESTED
    state.save(update_fields=['status', 'updated_at'])
    OrderStatusLog.objects.create(order=order, from_status=order.status, to_status=order.status, operator=operator, reason='老板申请取消未履行的剩余服务，等待客服核算退款')
    return state
