from apps.catalog.models import PlayerType

from .cancellation_models import OrderReplacementState, PlayerCancellationRecord


def replacement_payload(order):
    state = OrderReplacementState.objects.filter(order=order).first()
    if not state or state.status == OrderReplacementState.STATUS_RESOLVED:
        return {'active': False, 'status': state.status if state else ''}

    # 付款前普通陪玩退出只属于继续匹配，不能展示成紧急补位。
    if (
        state.mode == OrderReplacementState.MODE_PUBLIC
        and not order.paid
        and not order.timer_started_at
    ):
        return {'active': False, 'status': OrderReplacementState.STATUS_RESOLVED}

    current_players = order.order_players.count()
    missing_slots = max(0, int(order.required_players or 0) - current_players)
    phase = 'in_service' if order.timer_started_at else ('pre_service' if order.paid else 'matching')
    can_finish_cancel = bool(
        order.paid
        and state.status in {
            OrderReplacementState.STATUS_OPEN,
            OrderReplacementState.STATUS_CANCEL_REQUESTED,
        }
    )
    return {
        'active': True,
        'mode': state.mode,
        'mode_text': state.get_mode_display(),
        'status': state.status,
        'status_text': state.get_status_display(),
        'phase': phase,
        'missing_slots': missing_slots,
        'resume_status': state.resume_status,
        'remaining_minutes': state.remaining_minutes if order.paid else None,
        'cancelled_player_name': state.cancelled_player_name,
        'required_player_type_id': state.required_player_type_id,
        'required_player_type_name': state.required_player_type_name,
        'current_designation_id': state.current_designation_id,
        'can_reassign': state.mode == OrderReplacementState.MODE_TARGETED and state.status == OrderReplacementState.STATUS_OPEN,
        'can_publish_public': state.status == OrderReplacementState.STATUS_OPEN,
        'can_request_cancel': can_finish_cancel,
        # 旧版本可能已经留下 cancel_requested。新前端不再提供撤回入口，
        # 而是允许再次点击“取消剩余服务”直接完成自动退款。
        'can_revoke_cancel': False,
        'created_at': state.created_at,
        'updated_at': state.updated_at,
    }


def open_public_replacement(order):
    # 只有已付款后的人员退出才进入紧急公开补位；付款前继续走普通抢单。
    if not order.paid:
        return None
    return OrderReplacementState.objects.filter(
        order=order,
        mode=OrderReplacementState.MODE_PUBLIC,
        status=OrderReplacementState.STATUS_OPEN,
    ).first()


def can_player_take_replacement(order, player, state=None, check_cancellation_record=True):
    state = state or open_public_replacement(order)
    if not state:
        return False
    if order.order_players.count() >= order.required_players:
        return False
    if order.order_players.filter(player=player).exists():
        return False
    # 老板重新指定的人豁免取消案底检查：是老板点名要的，不是陪玩自己抢回
    if check_cancellation_record and PlayerCancellationRecord.objects.filter(order=order, player=player).exists():
        return False
    if state.required_player_type_id:
        required_type = PlayerType.objects.filter(id=state.required_player_type_id).first()
        if not required_type:
            return False
        player_priority = int(getattr(getattr(player, 'player_type', None), 'priority', 0) or 0)
        required_priority = int(required_type.priority or 0)
        if player_priority < required_priority:
            return False
    return True
