from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .designations import expire_due_designations, release_designation
from .models import Order


def can_access_order(order, user):
    if getattr(user, 'is_staff', False):
        return True
    return bool(getattr(user, 'is_authenticated', False) and order.boss_user_id == user.id)


def serialize_designation(item):
    try:
        avatar_url = item.player.user.client_profile.avatar_url or None
    except AttributeError:
        avatar_url = None
    return {
        'id': item.id,
        'player_id': item.player_id,
        'player_name': item.player.name,
        'player_type': item.player.player_type.name,
        'avatar_url': avatar_url,
        'is_online': item.player.is_online,
        'status': item.status,
        'status_text': item.get_status_display(),
        'extra_amount': item.extra_amount,
        'invited_at': item.invited_at,
        'responded_at': item.responded_at,
        'expires_at': item.expires_at,
        'can_release': item.status == 'pending' and item.order.status == Order.STATUS_WAITING,
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def designations(request, order_no):
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    if not can_access_order(order, request.user):
        return Response({'detail': '无权查看该订单'}, status=status.HTTP_403_FORBIDDEN)
    expire_due_designations(order=order)
    rows = (
        order.designations
        .select_related('order', 'player__player_type', 'player__user')
        .order_by('id')
    )
    return Response({
        'order_no': order.order_no,
        'results': [serialize_designation(item) for item in rows],
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def release(request, order_no, designation_id):
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    if not can_access_order(order, request.user):
        return Response({'detail': '无权操作该订单'}, status=status.HTTP_403_FORBIDDEN)
    designation = release_designation(order, designation_id, request.user)
    return Response({
        'message': '已取消指定，名额转为公开抢单',
        'designation': serialize_designation(designation),
    })
