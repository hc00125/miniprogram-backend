from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.common.permissions import IsApprovedPlayer, current_player
from apps.orders.models import Order, OrderStatusLog
from apps.orders.payment_deadlines import expire_due_unpaid_order, payment_deadline_payload
from apps.orders.room_entry_requeue import ROOM_ENTRY_TIMEOUT_REASON_PREFIX
from apps.orders.serializers import PlayerOrderDetailSerializer


@api_view(['GET'])
@permission_classes([IsApprovedPlayer])
def order_detail(request, order_no):
    """Return player order detail with the boss payment reservation window."""
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
    if not order.order_players.filter(player=player).exists():
        timed_out = OrderStatusLog.objects.filter(
            order=order,
            operator=request.user,
            reason__startswith=ROOM_ENTRY_TIMEOUT_REASON_PREFIX,
        ).exists()
        if timed_out:
            return Response(
                {
                    'detail': '您未在付款后10分钟内确认进入，本次已视为拒单，订单名额已转入公共抢单大厅',
                    'code': 'ROOM_ENTRY_TIMEOUT_REQUEUED',
                },
                status=status.HTTP_410_GONE,
            )
        return Response({'detail': '您不是这个订单的打手'}, status=status.HTTP_403_FORBIDDEN)

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
    data['cancel_reason'] = order.cancel_reason or ''
    data['canceled_at'] = order.canceled_at.isoformat() if order.canceled_at else None
    return Response(data)
