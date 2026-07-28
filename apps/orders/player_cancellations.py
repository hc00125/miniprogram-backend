from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.earnings.adjustments import create_and_apply_adjustment
from apps.earnings.models import WalletAdjustment
from apps.orders.models import Order, OrderDesignation, OrderPlayer, OrderStatusLog

from .cancellation_models import OrderReplacementState, PlayerCancellationRecord, PlayerDiscipline
from .discipline import cancellation_preview
from .replacements import replacement_payload


ACTIVE_CANCEL_STATUSES = {
    Order.STATUS_WAITING,
    Order.STATUS_PENDING_PAYMENT,
    Order.STATUS_READY_TO_START,
    Order.STATUS_IN_PROGRESS,
}


def _remaining_minutes(order, booked_hours, now):
    total_seconds = int(booked_hours * Decimal('3600'))
    if not order.timer_started_at:
        return max(1, int(booked_hours * Decimal('60')))
    elapsed = max(0, int((now - order.timer_started_at).total_seconds()) - int(order.paused_duration or 0))
    return max(1, (max(0, total_seconds - elapsed) + 59) // 60)


def _set_replacement_state(order, record, relation, now):
    remaining_count = order.order_players.exclude(pk=relation.pk).count()
    missing_slots = max(1, order.required_players - remaining_count)
    state, _ = OrderReplacementState.objects.select_for_update().get_or_create(
        order=order,
        defaults={
            'mode': record.replacement_mode,
            'status': OrderReplacementState.STATUS_OPEN,
            'missing_slots': missing_slots,
            'resume_status': record.previous_order_status,
        },
    )
    state.mode = record.replacement_mode
    state.status = OrderReplacementState.STATUS_OPEN
    state.missing_slots = missing_slots
    state.resume_status = record.previous_order_status
    state.remaining_minutes = _remaining_minutes(order, record.booked_hours_snapshot, now)
    state.cancelled_player_name = record.player.name
    state.required_player_type_id = record.player_type_id_snapshot
    state.required_player_type_name = record.player_type_name_snapshot
    state.latest_cancellation = record
    state.current_designation = None
    state.resolved_at = None
    state.save()
    return state


def get_cancel_preview(order, player):
    relation = OrderPlayer.objects.filter(order=order, player=player).select_related('player__player_type').first()
    if not relation:
        raise ValidationError({'detail': '您当前不在该订单阵容中'})
    if order.status not in ACTIVE_CANCEL_STATUSES:
        raise ValidationError({'detail': '当前订单状态不能取消接单'})
    result = cancellation_preview(order, relation, player)
    return {
        'order_no': order.order_no,
        'stage': result['stage'],
        'stage_text': result['stage_text'],
        'used_free_chance': result['used_free_chance'],
        'fine_rmb': result['fine_rmb'],
        'fine_fish': result['fine_fish'],
        'booked_hours': result['booked_hours'],
        'suspended_until': result['suspended_until'],
        'replacement_mode': result['replacement_mode'],
        'replacement_text': result['replacement_text'],
    }


@transaction.atomic
def cancel_player_order(order_no, player, reason, operator=None):
    reason = (reason or '').strip()
    if len(reason) < 2:
        raise ValidationError({'reason': '请填写取消原因'})

    order = Order.objects.select_for_update().filter(order_no=order_no).first()
    if not order:
        raise ValidationError({'detail': '订单不存在'})
    if order.status not in ACTIVE_CANCEL_STATUSES:
        raise ValidationError({'detail': '当前订单状态不能取消接单'})

    relation = (
        OrderPlayer.objects.select_for_update()
        .select_related('player__player_type')
        .filter(order=order, player=player)
        .first()
    )
    if not relation:
        raise ValidationError({'detail': '您当前不在该订单阵容中'})

    now = timezone.now()
    preview = cancellation_preview(order, relation, player, now)
    previous_status = order.status
    player_type = getattr(player, 'player_type', None)
    record = PlayerCancellationRecord.objects.create(
        order=order,
        player=player,
        stage=preview['stage'],
        replacement_mode=preview['replacement_mode'],
        reason=reason,
        previous_order_status=previous_status,
        was_designated=bool(relation.is_designated),
        player_type_id_snapshot=getattr(player_type, 'id', None),
        player_type_name_snapshot=getattr(player_type, 'name', '') or '',
        booked_hours_snapshot=preview['booked_hours'],
        used_free_chance=preview['used_free_chance'],
        fine_rmb=preview['fine_rmb'],
        fine_fish=preview['fine_fish'],
        suspended_until=preview['suspended_until'],
    )

    if preview['fine_fish'] > 0:
        adjustment = create_and_apply_adjustment(
            player=player,
            adjustment_type=WalletAdjustment.TYPE_PENALTY,
            amount=preview['fine_fish'],
            reason=f'订单 {order.order_no} {preview["stage_text"]}：{reason}',
            order=order,
            operator=operator,
        )
        record.wallet_adjustment = adjustment
        record.deducted_fish = max(Decimal('0.00'), -adjustment.available_delta)
        record.debt_fish = max(Decimal('0.00'), adjustment.debt_delta)
        record.save(update_fields=['wallet_adjustment', 'deducted_fish', 'debt_fish'])

    discipline, _ = PlayerDiscipline.objects.select_for_update().get_or_create(player=player)
    if not discipline.suspended_until or discipline.suspended_until < preview['suspended_until']:
        discipline.suspended_until = preview['suspended_until']
        discipline.save(update_fields=['suspended_until', 'updated_at'])

    state = _set_replacement_state(order, record, relation, now)
    OrderDesignation.objects.filter(
        order=order,
        player=player,
        status__in=[OrderDesignation.STATUS_PENDING, OrderDesignation.STATUS_ACCEPTED],
    ).update(status=OrderDesignation.STATUS_CANCELLED, responded_at=now)

    relation.delete()
    player.total_orders = max(0, int(player.total_orders or 0) - 1)
    player.is_online = False
    player.save(update_fields=['total_orders', 'is_online'])

    update_fields = []
    if order.fulfillment_mode == Order.FULFILLMENT_MODE_TARGETED and order.target_player_id == player.id:
        order.target_player = None
        order.target_player_name_snapshot = ''
        update_fields.extend(['target_player', 'target_player_name_snapshot'])

    if state.mode == OrderReplacementState.MODE_PUBLIC:
        if previous_status != Order.STATUS_IN_PROGRESS:
            order.status = Order.STATUS_WAITING
            update_fields.append('status')
        if previous_status == Order.STATUS_PENDING_PAYMENT and not order.paid:
            from apps.payments.services import close_unpaid_payments_for_order
            close_unpaid_payments_for_order(order, reason='陪玩取消接单，阵容需要重新补齐')
    elif previous_status != Order.STATUS_IN_PROGRESS:
        order.status = Order.STATUS_WAITING
        update_fields.append('status')

    if update_fields:
        order.save(update_fields=list(dict.fromkeys(update_fields)))

    from .designations import sync_designated_players_snapshot
    sync_designated_players_snapshot(order)

    fine_text = '使用免罚机会' if preview['used_free_chance'] else f'罚款¥{preview["fine_rmb"]}（{preview["fine_fish"]}鱼干）'
    until_text = timezone.localtime(preview['suspended_until']).strftime('%Y-%m-%d %H:%M')
    OrderStatusLog.objects.create(
        order=order,
        from_status=previous_status,
        to_status=order.status,
        operator=operator,
        reason=f'{player.name}{preview["stage_text"]}，按拒单处理；暂停接单至 {until_text}；{fine_text}；{preview["replacement_text"]}',
    )

    return {
        'message': '已取消接单',
        'order_no': order.order_no,
        'record_id': record.id,
        'stage': record.stage,
        'stage_text': record.get_stage_display(),
        'used_free_chance': record.used_free_chance,
        'fine_rmb': record.fine_rmb,
        'fine_fish': record.fine_fish,
        'deducted_fish': record.deducted_fish,
        'debt_fish': record.debt_fish,
        'suspended_until': record.suspended_until,
        'replacement': replacement_payload(order),
    }
