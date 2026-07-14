from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.common.permissions import IsApprovedPlayer, current_player
from apps.orders.designations import accept_designation, decline_designation, expire_due_designations
from apps.orders.models import Order, OrderDesignation
from apps.orders.serializers import AvailableOrderSerializer


def django_operator(user):
    return user if getattr(user, 'is_authenticated', False) and hasattr(user, 'pk') else None


@api_view(['GET'])
@permission_classes([IsApprovedPlayer])
def invitations(request):
    player = current_player(request.user)
    expire_due_designations()
    rows = (
        OrderDesignation.objects
        .filter(
            player=player,
            status=OrderDesignation.STATUS_PENDING,
            order__status=Order.STATUS_WAITING,
            order__order_type=Order.ORDER_TYPE_NORMAL,
        )
        .select_related('order__package', 'order__addon')
        .prefetch_related('order__order_players')
        .order_by('expires_at', 'id')
    )
    results = []
    for designation in rows:
        data = AvailableOrderSerializer(
            designation.order,
            context={'player': player},
        ).data
        data.update({
            'designation_id': designation.id,
            'designation_status': designation.status,
            'designation_status_text': designation.get_status_display(),
            'designation_expires_at': designation.expires_at,
            'can_accept_designation': True,
            'can_grab': False,
            'is_designated': True,
        })
        results.append(data)
    return Response(results)


@api_view(['POST'])
@permission_classes([IsApprovedPlayer])
def accept(request, order_no):
    try:
        order = accept_designation(
            order_no,
            current_player(request.user),
            django_operator(request.user),
        )
    except Order.DoesNotExist:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    return Response({
        'message': '已接受指定邀请',
        'order_no': order.order_no,
        'status': order.status,
    })


@api_view(['POST'])
@permission_classes([IsApprovedPlayer])
def decline(request, order_no):
    try:
        order = decline_designation(
            order_no,
            current_player(request.user),
            django_operator(request.user),
        )
    except Order.DoesNotExist:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    return Response({
        'message': '已拒绝指定邀请，名额已转为公开抢单',
        'order_no': order.order_no,
        'status': order.status,
    })
