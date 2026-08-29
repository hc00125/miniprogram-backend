from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.common.permissions import IsApprovedPlayer, current_player
from apps.orders.models import Order
from apps.orders.payment_deadlines import expire_due_unpaid_order, payment_deadline_payload
from apps.orders.replacements import replacement_payload
from apps.orders.serializers import PlayerOrderDetailSerializer


@api_view(['GET'])
@permission_classes([IsApprovedPlayer])
def order_detail(request, order_no):
    """Return player order detail with payment and replacement state."""
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
        return Response({'detail': '您不是这个订单的打手'}, status=status.HTTP_403_FORBIDDEN)

    # 兼容旧版本遗留的进入房间倒计时数据。新规则没有截止时间，也不会因超时释放名额。
    if relation.room_join_deadline is not None or relation.room_join_status == relation.ROOM_ENTRY_OVERDUE:
        relation.room_join_deadline = None
        if relation.room_join_status == relation.ROOM_ENTRY_OVERDUE:
            relation.room_join_status = relation.ROOM_ENTRY_PENDING
        relation.save(update_fields=['room_join_deadline', 'room_join_status'])

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
