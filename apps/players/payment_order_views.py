from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.common.permissions import IsApprovedPlayer, current_player
from apps.orders.models import Order
from apps.orders.payment_deadlines import expire_due_unpaid_order, payment_deadline_payload
from apps.orders.serializers import PlayerOrderDetailSerializer


@api_view(['GET'])
@permission_classes([IsApprovedPlayer])
def order_detail(request, order_no):
    """Return the player-facing order detail with the boss payment window.

    Players need to distinguish an active ten-minute lineup reservation from
    the short WeChat reconciliation phase. Refreshing this endpoint also keeps
    the unpaid-order expiry fallback consistent with the boss detail page.
    """
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
    return Response(data)
