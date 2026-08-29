from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.common.permissions import IsApprovedPlayer, current_player
from apps.orders.models import Order
from apps.orders.player_cancellations import cancel_player_order, get_cancel_preview

from .views import django_operator


@api_view(['GET'])
@permission_classes([IsApprovedPlayer])
def cancel_preview(request, order_no):
    player = current_player(request.user)
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    return Response(get_cancel_preview(order, player))


@api_view(['POST'])
@permission_classes([IsApprovedPlayer])
def cancel_order(request, order_no):
    player = current_player(request.user)
    result = cancel_player_order(
        order_no,
        player,
        request.data.get('reason'),
        django_operator(request.user),
    )
    return Response(result)
