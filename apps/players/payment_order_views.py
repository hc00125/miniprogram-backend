from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.common.permissions import IsApprovedPlayer, current_player
from apps.orders.models import Order, OrderPlayer, OrderStatusLog
from apps.orders.payment_deadlines import expire_due_unpaid_order, payment_deadline_payload
from apps.orders.replacements import replacement_payload
from apps.orders.room_entry_requeue import (
    ROOM_ENTRY_TIMEOUT_REASON_PREFIX,
    expire_room_entry_relation,
)
from apps.orders.serializers import PlayerOrderDetailSerializer


ROOM_TIMEOUT_PAYLOAD = {
    'detail': '您未在付款后10分钟内确认进入，本次已视为拒单，订单名额已转入公共抢单大厅',
    'code': 'ROOM_ENTRY_TIMEOUT_REQUEUED',
}


@api_view(['GET'])
@permission_classes([IsApprovedPlayer])
def order_detail(request, order_no):
    """Return player order detail with payment and urgent replacement state."""
    player = current_player(request.user)
    order = (
        Order.objects
        .filter(order_no=order_no)
        .select_related('package', 'addon', 'boss_user')
        .prefetch_related('order_players__player__player_type')
        .first()
    )
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)

    relation = order.order_players.filter(player=player).first()
    if not relation:
        timed_out = OrderStatusLog.objects.filter(
            order=order,
            operator=request.user,
            reason__startswith=ROOM_ENTRY_TIMEOUT_REASON_PREFIX,
        ).exists()
        if timed_out:
            return Response(ROOM_TIMEOUT_PAYLOAD, status=status.HTTP_410_GONE)
        return Response({'detail': '您不是这个订单的打手'}, status=status.HTTP_403_FORBIDDEN)

    now = timezone.now()
    if (
        relation.room_join_status in {OrderPlayer.ROOM_ENTRY_PENDING, OrderPlayer.ROOM_ENTRY_OVERDUE}
        and relation.room_join_deadline
        and relation.room_join_deadline <= now
        and expire_room_entry_relation(relation.id, now=now)
    ):
        return Response(ROOM_TIMEOUT_PAYLOAD, status=status.HTTP_410_GONE)

    if order.status == Order.STATUS_PENDING_PAYMENT and not order.paid:
        if expire_due_unpaid_order(order):
            order = (
                Order.objects
                .filter(order_no=order_no)
                .select_related('package', 'addon', 'boss_user')
                .prefetch_related('order_players__player__player_type')
                .first()
            )

    data = PlayerOrderDetailSerializer(order).data
    data.update(payment_deadline_payload(order))
    data['replacement'] = replacement_payload(order)
    data['cancel_reason'] = order.cancel_reason or ''
    data['canceled_at'] = order.canceled_at.isoformat() if order.canceled_at else None
    return Response(data)
