from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Order, Rating


def is_admin_user(user):
    return bool(user and getattr(user, 'is_authenticated', False) and getattr(user, 'is_staff', False))


def can_access_order(order, user):
    if is_admin_user(user):
        return True
    return bool(user and getattr(user, 'is_authenticated', False) and order.boss_user_id == user.id)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def order_ratings(request, order_no):
    order = (
        Order.objects
        .filter(order_no=order_no)
        .select_related('boss_user', 'parent_order')
        .prefetch_related('order_players')
        .first()
    )
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    if not can_access_order(order, request.user):
        return Response({'detail': '无权查看该订单评价'}, status=status.HTTP_403_FORBIDDEN)
    if order.order_type != Order.ORDER_TYPE_NORMAL:
        return Response(
            {'detail': '续单不单独评价，请查看原订单评价状态'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    player_ids = list(order.order_players.order_by('id').values_list('player_id', flat=True))
    ratings = list(
        Rating.objects
        .filter(order=order)
        .select_related('player')
        .order_by('id')
    )
    rated_player_ids = [item.player_id for item in ratings]
    rated_set = set(rated_player_ids)

    return Response({
        'order_no': order.order_no,
        'rated_player_ids': rated_player_ids,
        'remaining_player_ids': [player_id for player_id in player_ids if player_id not in rated_set],
        'all_rated': bool(player_ids) and all(player_id in rated_set for player_id in player_ids),
        'results': [
            {
                'id': item.id,
                'player_id': item.player_id,
                'player_name': item.player.name,
                'rating': item.rating,
                'comment': item.comment or '',
                'created_at': item.created_at,
            }
            for item in ratings
        ],
    })
