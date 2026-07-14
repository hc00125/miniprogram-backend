from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.common.permissions import IsApprovedPlayer, current_player
from apps.orders.models import Order, OrderPlayer, OrderStatusLog


@api_view(['POST'])
@permission_classes([IsApprovedPlayer])
@transaction.atomic
def confirm_room_entry(request, order_no):
    player = current_player(request.user)
    order = Order.objects.select_for_update().filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    if order.status in {Order.STATUS_COMPLETED, Order.STATUS_CANCELLED}:
        return Response({'detail': '当前订单已经结束，不能确认进入房间'}, status=status.HTTP_400_BAD_REQUEST)

    relation = (
        OrderPlayer.objects
        .select_for_update()
        .filter(order=order, player=player)
        .first()
    )
    if not relation:
        return Response({'detail': '您不是这个订单的陪玩师'}, status=status.HTTP_403_FORBIDDEN)

    now = timezone.now()
    relation.refresh_room_join_status(now=now)
    if relation.room_join_status in {
        OrderPlayer.ROOM_ENTRY_CONFIRMED,
        OrderPlayer.ROOM_ENTRY_LATE_CONFIRMED,
    }:
        return Response({
            'message': '已经确认进入房间',
            'room_join_status': relation.room_join_status,
            'room_join_status_text': relation.get_room_join_status_display(),
            'room_join_confirmed_at': relation.room_join_confirmed_at,
            'room_join_deadline': relation.room_join_deadline,
        })

    was_late = bool(relation.room_join_deadline and now > relation.room_join_deadline)
    relation.room_join_status = (
        OrderPlayer.ROOM_ENTRY_LATE_CONFIRMED if was_late else OrderPlayer.ROOM_ENTRY_CONFIRMED
    )
    relation.room_join_confirmed_at = now
    relation.save(update_fields=['room_join_status', 'room_join_confirmed_at'])
    OrderStatusLog.objects.create(
        order=order,
        from_status=order.status,
        to_status=order.status,
        operator=request.user if getattr(request.user, 'is_authenticated', False) else None,
        reason=(
            f'陪玩 {player.name} 超时后确认进入老板房间'
            if was_late
            else f'陪玩 {player.name} 已确认进入老板房间'
        ),
    )
    return Response({
        'message': '已记录进入房间' if not was_late else '已记录进入房间，本次为超时确认，等待管理员核实',
        'room_join_status': relation.room_join_status,
        'room_join_status_text': relation.get_room_join_status_display(),
        'room_join_confirmed_at': relation.room_join_confirmed_at,
        'room_join_deadline': relation.room_join_deadline,
        'was_late': was_late,
    })
