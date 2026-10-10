from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.orders.models import Order
from apps.orders.services import pause_order, resume_order


def _can_control_timer(order, user):
    if getattr(user, 'is_staff', False):
        return True
    return bool(
        user
        and getattr(user, 'is_authenticated', False)
        and order.boss_user_id == user.id
    )


def _order_or_error(order_no):
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        return None, Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    return order, None


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def pause(request, order_no):
    order, error = _order_or_error(order_no)
    if error:
        return error
    if not _can_control_timer(order, request.user):
        return Response({'detail': '无权操作该订单'}, status=status.HTTP_403_FORBIDDEN)
    pause_order(order)
    return Response({'message': '计时已暂停'})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def resume(request, order_no):
    order, error = _order_or_error(order_no)
    if error:
        return error
    if not _can_control_timer(order, request.user):
        return Response({'detail': '无权操作该订单'}, status=status.HTTP_403_FORBIDDEN)
    order = resume_order(order)
    return Response({'message': '计时已继续', 'paused_duration': order.paused_duration})
