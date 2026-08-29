from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.common.permissions import IsApprovedPlayer, current_player
from apps.orders.designations import accept_designation, decline_designation, expire_due_designations
from apps.orders.discipline import discipline_block_reason
from apps.orders.models import Order, OrderDesignation
from apps.orders.replacements import (
    accept_replacement_designation,
    decline_replacement_designation,
    open_targeted_replacement,
)
from apps.orders.serializers import AvailableOrderSerializer

from .escort_qualification import escort_order_block_reason, order_requires_escort_qualification


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
            order__status__in=[Order.STATUS_WAITING, Order.STATUS_IN_PROGRESS],
            order__order_type=Order.ORDER_TYPE_NORMAL,
        )
        .select_related('order__package', 'order__addon')
        .prefetch_related('order__order_players')
        .order_by('expires_at', 'id')
    )
    results = []
    for designation in rows:
        order = designation.order
        escort_reason = escort_order_block_reason(order, player)
        permission_reason = ''
        if not player.can_accept_orders or not player.can_be_designated:
            permission_reason = '管理员已暂停您的接单或被指定权限'
        block_reason = discipline_block_reason(player) or permission_reason or escort_reason
        data = AvailableOrderSerializer(order, context={'player': player}).data
        data.update({
            'designation_id': designation.id,
            'designation_status': designation.status,
            'designation_status_text': designation.get_status_display(),
            'designation_expires_at': designation.expires_at,
            'requires_escort_qualification': order_requires_escort_qualification(order),
            'can_accept_designation': not block_reason,
            'permission_block_reason': block_reason,
            'can_grab': False,
            'is_designated': True,
            'is_replacement': bool(open_targeted_replacement(order)),
        })
        results.append(data)
    return Response(results)


@api_view(['POST'])
@permission_classes([IsApprovedPlayer])
def accept(request, order_no):
    player = current_player(request.user)
    block_reason = discipline_block_reason(player)
    if block_reason:
        return Response({'detail': block_reason}, status=status.HTTP_403_FORBIDDEN)
    if not player.can_accept_orders:
        return Response({'detail': '管理员已暂停您的接单权限'}, status=status.HTTP_403_FORBIDDEN)
    if not player.can_be_designated:
        return Response({'detail': '管理员已暂停您的被指定权限'}, status=status.HTTP_403_FORBIDDEN)
    try:
        order = Order.objects.select_related('package').get(order_no=order_no)
    except Order.DoesNotExist:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    escort_reason = escort_order_block_reason(order, player)
    if escort_reason:
        return Response({'detail': escort_reason}, status=status.HTTP_403_FORBIDDEN)

    is_replacement = bool(open_targeted_replacement(order))
    if is_replacement:
        order = accept_replacement_designation(order_no, player, django_operator(request.user))
    else:
        order = accept_designation(order_no, player, django_operator(request.user))
    return Response({
        'message': '已接受补位邀请' if is_replacement else '已接受指定邀请',
        'order_no': order.order_no,
        'status': order.status,
    })


@api_view(['POST'])
@permission_classes([IsApprovedPlayer])
def decline(request, order_no):
    player = current_player(request.user)
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    is_replacement = bool(open_targeted_replacement(order))
    try:
        if is_replacement:
            order = decline_replacement_designation(order_no, player, django_operator(request.user))
        else:
            order = decline_designation(order_no, player, django_operator(request.user))
    except Order.DoesNotExist:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)

    if is_replacement:
        message = '已拒绝补位邀请，老板可重新选择其他陪玩'
    else:
        message = (
            '已拒绝指定服务，订单已取消并进入退款流程'
            if order.fulfillment_mode == Order.FULFILLMENT_MODE_TARGETED
            else '已拒绝指定邀请，名额已转为公开抢单'
        )
    return Response({
        'message': message,
        'order_no': order.order_no,
        'status': order.status,
    })
