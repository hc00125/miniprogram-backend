from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.catalog.models import Addon, Package, PackageGroup, PlayerType
from apps.catalog.serializers import AddonSerializer, PackageGroupSerializer, PackageSerializer, PlayerTypeSerializer
from apps.orders.models import Order, Rating
from apps.orders.serializers import BossOrderDetailSerializer, BossOrderListSerializer, OrderCreateSerializer, RatingCreateSerializer
from apps.orders.services import cancel_order as cancel_order_service, create_order as create_order_service, pause_order, resume_order
from apps.players.models import Player


@api_view(['GET'])
@permission_classes([AllowAny])
def packages(request):
    qs = Package.objects.filter(is_active=True).select_related('group')
    return Response(PackageSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def package_groups(request):
    qs = PackageGroup.objects.filter(is_active=True).order_by('sort_order', 'id')
    return Response(PackageGroupSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def addons(request):
    qs = Addon.objects.filter(is_active=True).order_by('priority', 'id')
    return Response(AddonSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def player_types(request):
    qs = PlayerType.objects.filter(is_active=True).order_by('priority', 'id')
    return Response(PlayerTypeSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def online_players(request):
    players = Player.objects.filter(is_online=True).select_related('player_type')
    result = []
    for player in players:
        active_order = player.order_players.filter(order__status=Order.STATUS_IN_PROGRESS).exists()
        result.append({
            'id': player.id,
            'name': player.name,
            'type_id': player.player_type_id,
            'type_name': player.player_type.name,
            'price_extra': player.player_type.price_extra or 0,
            'avg_rating': player.avg_rating,
            'total_orders': player.total_orders,
            'status': '接单中' if active_order else '在线',
        })
    return Response(result)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_order(request):
    serializer = OrderCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    order = create_order_service(serializer.validated_data, request.user)
    return Response({
        'order_no': order.order_no,
        'status': order.status,
        'total_price': order.total_amount,
        'message': '订单创建成功，等待打手接单',
    })


@api_view(['GET'])
@permission_classes([AllowAny])
def order_detail(request, order_no):
    order = Order.objects.filter(order_no=order_no).select_related('package', 'addon').prefetch_related('order_players__player__player_type').first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    return Response(BossOrderDetailSerializer(order).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def boss_orders(request, boss_wechat):
    qs = Order.objects.filter(boss_wechat=boss_wechat).select_related('package').order_by('-created_at')[:20]
    return Response(BossOrderListSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_orders(request):
    qs = Order.objects.filter(boss_user=request.user).select_related('package').order_by('-created_at')[:50]
    return Response(BossOrderListSerializer(qs, many=True).data)


@api_view(['POST'])
@permission_classes([AllowAny])
def cancel_order(request, order_no):
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    cancel_order_service(order, request.data.get('reason'), request.user if getattr(request.user, 'is_authenticated', False) else None)
    return Response({'message': '订单已取消', 'order_no': order_no})


@api_view(['POST'])
@permission_classes([AllowAny])
def rate_player(request, order_no):
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    if order.status not in {Order.STATUS_COMPLETED, Order.STATUS_PENDING_PAYMENT}:
        return Response({'detail': '只能评价已完成的订单'}, status=status.HTTP_400_BAD_REQUEST)
    serializer = RatingCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    player = Player.objects.filter(id=serializer.validated_data['player_id']).first()
    if not player:
        return Response({'detail': '打手不存在'}, status=status.HTTP_404_NOT_FOUND)
    if Rating.objects.filter(order=order, player=player).exists():
        return Response({'detail': '已评价过该打手'}, status=status.HTTP_400_BAD_REQUEST)
    Rating.objects.create(order=order, player=player, rating=serializer.validated_data['rating'], comment=serializer.validated_data.get('comment'))
    ratings = Rating.objects.filter(player=player)
    player.rating_count = ratings.count()
    player.total_rating = sum(item.rating for item in ratings)
    player.save(update_fields=['rating_count', 'total_rating'])
    return Response({'message': '评价成功'})


@api_view(['POST'])
@permission_classes([AllowAny])
def self_confirm_payment(request, order_no):
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    if order.status != Order.STATUS_PENDING_PAYMENT:
        return Response({'detail': '订单状态不正确'}, status=status.HTTP_400_BAD_REQUEST)
    actual_amount = request.data.get('actual_amount')
    if actual_amount is not None and float(actual_amount) > 0:
        order.total_amount = round(float(actual_amount), 2)
    elif not order.total_amount:
        order.total_amount = order.total_price_per_hour
    order.paid = True
    order.payment_method = 'self_confirm'
    order.payment_confirmed_at = timezone.now()
    order.save(update_fields=['total_amount', 'paid', 'payment_method', 'payment_confirmed_at'])
    return Response({'message': '支付确认成功', 'order_no': order_no})


@api_view(['POST'])
@permission_classes([AllowAny])
def pause(request, order_no):
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    pause_order(order)
    return Response({'message': '计时已暂停'})


@api_view(['POST'])
@permission_classes([AllowAny])
def resume(request, order_no):
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    order = resume_order(order)
    return Response({'message': '计时已继续', 'paused_duration': order.paused_duration})
