from django.db.models import Q
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.common.permissions import IsApprovedPlayer, current_player
from apps.orders.cancellation_models import OrderReplacementState
from apps.orders.discipline import discipline_block_reason
from apps.orders.models import Order
from apps.orders.serializers import AvailableOrderSerializer, OrderActionSerializer
from apps.orders.services import can_player_grab_order, grab_order as grab_order_service

from .escort_qualification import escort_order_block_reason
from .views import django_operator


@api_view(['POST'])
@permission_classes([IsApprovedPlayer])
def update_online_status(request):
    player = current_player(request.user)
    is_online = request.data.get('is_online')
    if not isinstance(is_online, bool):
        return Response({'detail': 'is_online 必须为布尔值 true/false'}, status=status.HTTP_400_BAD_REQUEST)
    block_reason = discipline_block_reason(player)
    if is_online and (not player.can_accept_orders or block_reason):
        return Response({'detail': block_reason or '管理员已暂停您的接单权限'}, status=status.HTTP_403_FORBIDDEN)
    player.is_online = is_online
    player.save(update_fields=['is_online', 'updated_at'])
    return Response({
        'id': player.id,
        'name': player.name,
        'is_online': player.is_online,
        'status': '在线' if player.is_online else '离线',
    })


@api_view(['GET'])
@permission_classes([IsApprovedPlayer])
def available_orders(request):
    player = current_player(request.user)
    if not player.can_accept_orders or discipline_block_reason(player):
        return Response([])
    replacement_order_ids = OrderReplacementState.objects.filter(
        mode=OrderReplacementState.MODE_PUBLIC,
        status=OrderReplacementState.STATUS_OPEN,
    ).values_list('order_id', flat=True)
    orders = (
        Order.objects
        .filter(
            Q(status=Order.STATUS_WAITING) | Q(id__in=replacement_order_ids),
            order_type=Order.ORDER_TYPE_NORMAL,
        )
        .select_related('package', 'addon')
        .prefetch_related('order_players')
        .distinct()
    )
    result = []
    for order in orders:
        if escort_order_block_reason(order, player):
            continue
        order.can_player_grab = can_player_grab_order(order, player)
        if order.can_player_grab:
            result.append(order)
    return Response(AvailableOrderSerializer(result, many=True, context={'player': player}).data)


@api_view(['POST'])
@permission_classes([IsApprovedPlayer])
def grab(request):
    player = current_player(request.user)
    block_reason = discipline_block_reason(player)
    if not player.can_accept_orders or block_reason:
        return Response({'detail': block_reason or '管理员已暂停您的接单权限'}, status=status.HTTP_403_FORBIDDEN)
    serializer = OrderActionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        order = grab_order_service(
            serializer.validated_data['order_no'],
            player,
            django_operator(request.user),
        )
    except Order.DoesNotExist:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    return Response({'message': '接单成功', 'order_no': order.order_no, 'status': order.status})
