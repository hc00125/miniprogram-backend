from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.money import money

from .batch_serializers import CartBatchOrderCreateSerializer
from .services import create_cart_orders


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_cart_order_batch(request):
    serializer = CartBatchOrderCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    orders = create_cart_orders(
        cart_item_ids=data['cart_item_ids'],
        validated_data=data,
        user=request.user,
    )
    total_amount = sum((money(order.total_amount) for order in orders), money(0))
    return Response({
        'order_count': len(orders),
        'order_nos': [order.order_no for order in orders],
        'total_amount': float(total_amount),
        'orders': [
            {
                'order_no': order.order_no,
                'package_name': order.package_name_snapshot or order.package.name,
                'status': order.status,
                'required_players': order.required_players,
                'total_amount': order.total_amount,
            }
            for order in orders
        ],
        'message': f'已发布 {len(orders)} 个独立订单，请分别等待陪玩接单与后续支付',
    }, status=status.HTTP_201_CREATED)
