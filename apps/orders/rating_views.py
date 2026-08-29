from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.content_security import SCENE_COMMENT, ensure_text_safe, user_openid
from apps.players.models import Player

from .models import Order, Rating
from .serializers import RatingCreateSerializer


def is_admin_user(user):
    return bool(user and getattr(user, 'is_authenticated', False) and getattr(user, 'is_staff', False))


def can_access_order(order, user):
    if is_admin_user(user):
        return True
    return bool(user and getattr(user, 'is_authenticated', False) and order.boss_user_id == user.id)


def build_rating_status(order):
    player_ids = list(order.order_players.order_by('id').values_list('player_id', flat=True))
    ratings = list(
        Rating.objects
        .filter(order=order)
        .select_related('player')
        .order_by('id')
    )
    rated_player_ids = [item.player_id for item in ratings]
    rated_set = set(rated_player_ids)
    return {
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
    }


@api_view(['GET', 'POST'])
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
        return Response({'detail': '无权操作该订单评价'}, status=status.HTTP_403_FORBIDDEN)
    if order.order_type != Order.ORDER_TYPE_NORMAL:
        return Response(
            {'detail': '续单不单独评价，请查看原订单评价状态'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if request.method == 'GET':
        return Response(build_rating_status(order))

    if order.status != Order.STATUS_COMPLETED or not order.paid:
        return Response({'detail': '只能评价已完成且已支付的订单'}, status=status.HTTP_400_BAD_REQUEST)

    serializer = RatingCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    comment = serializer.validated_data.get('comment') or ''
    ensure_text_safe(comment, openid=user_openid(request.user), scene=SCENE_COMMENT)

    player = Player.objects.filter(id=serializer.validated_data['player_id']).first()
    if not player:
        return Response({'detail': '陪玩不存在'}, status=status.HTTP_404_NOT_FOUND)
    if not order.order_players.filter(player=player).exists():
        return Response({'detail': '该陪玩不属于此订单'}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        rating, created = Rating.objects.get_or_create(
            order=order,
            player=player,
            defaults={
                'rating': serializer.validated_data['rating'],
                'comment': comment,
            },
        )
        if not created:
            return Response(
                {
                    'detail': '已评价过该陪玩，不能重复提交',
                    'existing_rating': {
                        'id': rating.id,
                        'player_id': rating.player_id,
                        'rating': rating.rating,
                        'comment': rating.comment or '',
                        'created_at': rating.created_at,
                    },
                },
                status=status.HTTP_409_CONFLICT,
            )

        player_ratings = Rating.objects.filter(player=player)
        player.rating_count = player_ratings.count()
        player.total_rating = sum(item.rating for item in player_ratings)
        player.save(update_fields=['rating_count', 'total_rating'])

    return Response(
        {
            'message': '评价成功',
            'rating': {
                'id': rating.id,
                'player_id': rating.player_id,
                'rating': rating.rating,
                'comment': rating.comment or '',
                'created_at': rating.created_at,
            },
        },
        status=status.HTTP_201_CREATED,
    )
