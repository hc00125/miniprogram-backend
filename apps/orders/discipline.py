from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.earnings.models import PlayerWallet
from apps.orders.models import Order, OrderPlayer

from .cancellation_models import PlayerCancellationRecord, PlayerDiscipline


FISH_PER_RMB = Decimal('10.00')
FINE_LADDER_RMB = [Decimal('20.00'), Decimal('50.00'), Decimal('100.00')]


def period_start(stage, now):
    if stage == PlayerCancellationRecord.STAGE_BEFORE_JOIN:
        local_date = timezone.localtime(now).date()
        local_midnight = datetime.combine(local_date, time.min)
        return timezone.make_aware(local_midnight, timezone.get_current_timezone())
    return now - timedelta(days=3)


def cancellation_stage(order, relation):
    joined = relation.room_join_status in {
        OrderPlayer.ROOM_ENTRY_CONFIRMED,
        OrderPlayer.ROOM_ENTRY_LATE_CONFIRMED,
    }
    if order.status == Order.STATUS_IN_PROGRESS or order.timer_started_at or joined:
        return PlayerCancellationRecord.STAGE_IN_SERVICE
    return PlayerCancellationRecord.STAGE_BEFORE_JOIN


def booked_hours(order):
    root = order.parent_order if order.order_type == Order.ORDER_TYPE_RENEWAL and order.parent_order_id else order
    renewal_hours = sum(
        Decimal(str(value or 0))
        for value in root.renewal_orders.filter(paid=True, status=Order.STATUS_COMPLETED).values_list('booked_hours', flat=True)
    )
    return max(Decimal('0.50'), Decimal(str(root.booked_hours or 1)) + renewal_hours)


def cancellation_preview(order, relation, player, now=None):
    now = now or timezone.now()
    stage = cancellation_stage(order, relation)
    records = PlayerCancellationRecord.objects.filter(
        player=player,
        stage=stage,
        created_at__gte=period_start(stage, now),
    )
    use_free_chance = not records.filter(used_free_chance=True).exists()
    charged_count = records.filter(used_free_chance=False, fine_rmb__gt=0).count()
    fine_rmb = Decimal('0.00') if use_free_chance else FINE_LADDER_RMB[min(charged_count, 2)]
    hours = booked_hours(order)
    replacement_mode = (
        PlayerCancellationRecord.REPLACEMENT_TARGETED
        if relation.is_designated or order.fulfillment_mode == Order.FULFILLMENT_MODE_TARGETED
        else PlayerCancellationRecord.REPLACEMENT_PUBLIC
    )
    return {
        'stage': stage,
        'stage_text': '进队前取消' if stage == PlayerCancellationRecord.STAGE_BEFORE_JOIN else '进队后/服务中取消',
        'used_free_chance': use_free_chance,
        'fine_rmb': fine_rmb,
        'fine_fish': fine_rmb * FISH_PER_RMB,
        'booked_hours': hours,
        'suspended_until': now + timedelta(hours=float(hours)),
        'replacement_mode': replacement_mode,
        'replacement_text': '重新进入抢单大厅' if replacement_mode == PlayerCancellationRecord.REPLACEMENT_PUBLIC else '等待老板重新指定或转公开补位',
    }


def discipline_block_reason(player, now=None):
    now = now or timezone.now()
    discipline = PlayerDiscipline.objects.filter(player=player).first()
    if discipline and discipline.suspended_until and discipline.suspended_until > now:
        until_text = timezone.localtime(discipline.suspended_until).strftime('%m-%d %H:%M')
        return f'拒单暂停接单中，预计 {until_text} 恢复'

    if PlayerCancellationRecord.objects.filter(player=player, debt_fish__gt=0).exists():
        debt = PlayerWallet.objects.filter(player=player).values_list('debt_balance', flat=True).first() or Decimal('0.00')
        if debt > 0:
            return f'尚有 {debt} 鱼干罚款欠款，请补齐后再接单'
    return ''


def ensure_player_can_accept(player):
    reason = discipline_block_reason(player)
    if reason:
        raise ValidationError({'detail': reason})


def can_player_grab_with_discipline(original_can_grab, order, player):
    if discipline_block_reason(player):
        return False
    if PlayerCancellationRecord.objects.filter(order=order, player=player).exists():
        return False
    return original_can_grab(order, player)
