from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .boss_views import can_access_order, forbidden_response, get_order_or_response
from .matching import extend_matching, matching_payload


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def continue_matching(request, order_no):
    order, error_response = get_order_or_response(order_no)
    if error_response:
        return error_response
    if not can_access_order(order, request.user):
        return forbidden_response()
    extend_matching(order, request.user)
    order.refresh_from_db()
    return Response({
        'message': '已继续等待匹配；公开抢单最晚在首次发布2小时后自动取消',
        'matching': matching_payload(order),
    })
