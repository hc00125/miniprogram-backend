from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.content_security import SCENE_SOCIAL, ensure_texts_safe, user_openid

from .listing_orders import create_listing_order
from .serializers import OrderCreateSerializer


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_order(request):
    try:
        listing_id = int(request.data.get('listing_id') or 0)
    except (TypeError, ValueError):
        listing_id = 0
    if listing_id <= 0:
        return Response({'detail': '缺少有效的陪玩师服务上架记录'}, status=status.HTTP_400_BAD_REQUEST)

    payload = request.data.copy()
    if hasattr(payload, 'pop'):
        payload.pop('listing_id', None)
    serializer = OrderCreateSerializer(data=payload)
    serializer.is_valid(raise_exception=True)

    ensure_texts_safe(
        [serializer.validated_data.get('game_id'), serializer.validated_data.get('boss_note')],
        openid=str(serializer.validated_data.get('boss_wechat') or '') or user_openid(request.user),
        scene=SCENE_SOCIAL,
    )

    order = create_listing_order(serializer.validated_data, listing_id, request.user)
    return Response({
        'order_no': order.order_no,
        'status': order.status,
        'total_price': order.total_amount,
        'message': '指定订单已创建，请完成支付',
    }, status=status.HTTP_201_CREATED)
