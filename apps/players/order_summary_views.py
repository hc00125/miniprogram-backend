from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.common.permissions import IsApprovedPlayer, current_player
from apps.orders.models import Order
from apps.orders.serializers import PlayerOrderListSerializer


def boss_name_for_order(order):
    profile = getattr(order.boss_user, 'client_profile', None)
    nickname = (getattr(profile, 'nickname', '') or '').strip()
    return nickname or '未设置昵称'


@api_view(['GET'])
@permission_classes([IsApprovedPlayer])
def my_orders(request):
    player = current_player(request.user)
    orders = list(
        Order.objects
        .filter(order_players__player=player)
        .select_related('package', 'addon', 'boss_user__client_profile')
        .order_by('-order_players__grab_time')
    )
    payload = PlayerOrderListSerializer(orders, many=True, context={'player': player}).data
    for order, item in zip(orders, payload):
        item['boss_name'] = boss_name_for_order(order)
    return Response(payload)
