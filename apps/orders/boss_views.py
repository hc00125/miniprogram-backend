from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.catalog.models import Addon, Package, PackageGroup, PlayerType
from apps.catalog.serializers import AddonSerializer, PackageGroupSerializer, PackageSerializer, PlayerTypeSerializer
from apps.orders.models import CartItem, Order, OrderStatusLog, Rating
from apps.orders.serializers import (
    BossOrderDetailSerializer,
    BossOrderListSerializer,
    CartItemCreateSerializer,
    CartItemQuantitySerializer,
    CartItemSerializer,
    OrderCreateSerializer,
    RatingCreateSerializer,
)
from apps.orders.services import cancel_order as cancel_order_service, create_order as create_order_service, pause_order, resume_order
from apps.payments.services import close_unpaid_payments_for_order
from apps.players.models import Player


def is_admin_user(user):
    return bool(user and getattr(user, 'is_authenticated', False) and getattr(user, 'is_staff', False))


def can_access_order(order, user):
    if is_admin_user(user):
        return True
    return bool(user and getattr(user, 'is_authenticated', False) and order.boss_user_id == user.id)


def forbidden_response():
    return Response({'detail': '无权操作该订单'}, status=status.HTTP_403_FORBIDDEN)


def get_order_or_response(order_no):
    order = Order.objects.filter(order_no=order_no).select_related('package', 'addon', 'boss_user').prefetch_related('order_players__player__player_type').first()
    if not order:
        return None, Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    return order, None


def get_cart_item_or_response(request, item_id):
    item = CartItem.objects.filter(id=item_id, user=request.user).select_related('package__group').first()
    if not item:
        return None, Response({'detail': '购物车商品不存在'}, status=status.HTTP_404_NOT_FOUND)
    return item, None


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


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def cart(request):
    if request.method == 'GET':
        qs = CartItem.objects.filter(user=request.user).select_related('package__group').order_by('-updated_at')
        return Response(CartItemSerializer(qs, many=True).data)

    serializer = CartItemCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    package = Package.objects.filter(id=data['package_id'], is_active=True).first()
    if not package:
        return Response({'detail': '商品不存在或已下架'}, status=status.HTTP_404_NOT_FOUND)

    spec_id = str(data.get('spec_id') or '')
    quantity = data.get('quantity') or 1
    item, created = CartItem.objects.get_or_create(
        user=request.user,
        package=package,
        spec_id=spec_id,
        defaults={
            'spec_name': data.get('spec_name') or '',
            'spec_display_name': data.get('spec_display_name') or data.get('spec_name') or '',
            'price': data['price'],
            'quantity': quantity,
            'image_url': data.get('image_url') or None,
            'description': data.get('description') or None,
        },
    )
    if not created:
        item.quantity = min(99, item.quantity + quantity)
        item.spec_name = data.get('spec_name') or item.spec_name
        item.spec_display_name = data.get('spec_display_name') or item.spec_display_name
        item.price = data['price']
        item.image_url = data.get('image_url') or item.image_url
        item.description = data.get('description') or item.description
        item.save(update_fields=['quantity', 'spec_name', 'spec_display_name', 'price', 'image_url', 'description', 'updated_at'])
    return Response(CartItemSerializer(item).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@api_view(['PUT', 'DELETE'])
@permission_classes([IsAuthenticated])
def cart_item(request, item_id):
    item, error_response = get_cart_item_or_response(request, item_id)
    if error_response:
        return error_response

    if request.method == 'DELETE':
        item.delete()
        return Response({'message': '已删除'})

    serializer = CartItemQuantitySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    item.quantity = serializer.validated_data['quantity']
    item.save(update_fields=['quantity', 'updated_at'])
    return Response(CartItemSerializer(item).data)


@api_view(['DELETE', 'POST'])
@permission_classes([IsAuthenticated])
def clear_cart(request):
    CartItem.objects.filter(user=request.user).delete()
    return Response({'message': '购物车已清空'})


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
@permission_classes([IsAuthenticated])
def order_detail(request, order_no):
    order, error_response = get_order_or_response(order_no)
    if error_response:
        return error_response
    if not can_access_order(order, request.user):
        return forbidden_response()
    return Response(BossOrderDetailSerializer(order).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def boss_orders(request, boss_wechat):
    if is_admin_user(request.user):
        qs = Order.objects.filter(boss_wechat=boss_wechat).select_related('package').order_by('-created_at')[:20]
    else:
        qs = Order.objects.filter(boss_user=request.user).select_related('package').order_by('-created_at')[:20]
    return Response(BossOrderListSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_orders(request):
    qs = Order.objects.filter(boss_user=request.user).select_related('package').order_by('-created_at')[:50]
    return Response(BossOrderListSerializer(qs, many=True).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def cancel_order(request, order_no):
    order, error_response = get_order_or_response(order_no)
    if error_response:
        return error_response
    if not can_access_order(order, request.user):
        return forbidden_response()
    if order.status not in {Order.STATUS_WAITING, Order.STATUS_IN_PROGRESS}:
        return Response({'detail': '当前状态无法取消'}, status=status.HTTP_400_BAD_REQUEST)
    reason = request.data.get('reason')
    close_unpaid_payments_for_order(order, reason=reason or '订单取消')
    cancel_order_service(order, reason, request.user)
    return Response({'message': '订单已取消', 'order_no': order_no})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def rate_player(request, order_no):
    order, error_response = get_order_or_response(order_no)
    if error_response:
        return error_response
    if not can_access_order(order, request.user):
        return forbidden_response()
    if order.status != Order.STATUS_COMPLETED or not order.paid:
        return Response({'detail': '只能评价已完成且已支付的订单'}, status=status.HTTP_400_BAD_REQUEST)
    serializer = RatingCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    player = Player.objects.filter(id=serializer.validated_data['player_id']).first()
    if not player:
        return Response({'detail': '打手不存在'}, status=status.HTTP_404_NOT_FOUND)
    if not order.order_players.filter(player=player).exists():
        return Response({'detail': '该打手不属于此订单'}, status=status.HTTP_400_BAD_REQUEST)
    if Rating.objects.filter(order=order, player=player).exists():
        return Response({'detail': '已评价过该打手'}, status=status.HTTP_400_BAD_REQUEST)
    Rating.objects.create(order=order, player=player, rating=serializer.validated_data['rating'], comment=serializer.validated_data.get('comment'))
    ratings = Rating.objects.filter(player=player)
    player.rating_count = ratings.count()
    player.total_rating = sum(item.rating for item in ratings)
    player.save(update_fields=['rating_count', 'total_rating'])
    return Response({'message': '评价成功'})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def self_confirm_payment(request, order_no):
    order, error_response = get_order_or_response(order_no)
    if error_response:
        return error_response
    if not can_access_order(order, request.user):
        return forbidden_response()
    if not is_admin_user(request.user) and not (settings.DEBUG or settings.ENABLE_MOCK_PAYMENT or settings.ENABLE_DEV_OPENID_LOGIN):
        return Response({'detail': '生产环境仅管理员可以手动确认支付'}, status=status.HTTP_403_FORBIDDEN)
    if order.status != Order.STATUS_PENDING_PAYMENT:
        return Response({'detail': '订单状态不正确'}, status=status.HTTP_400_BAD_REQUEST)
    old_status = order.status
    actual_amount = request.data.get('actual_amount')
    if actual_amount is not None and float(actual_amount) > 0:
        order.total_amount = round(float(actual_amount), 2)
    elif not order.total_amount:
        order.total_amount = order.total_price_per_hour
    order.paid = True
    order.status = Order.STATUS_COMPLETED
    order.payment_method = 'self_confirm'
    order.payment_confirmed_at = timezone.now()
    order.save(update_fields=['total_amount', 'paid', 'status', 'payment_method', 'payment_confirmed_at'])
    OrderStatusLog.objects.create(order=order, from_status=old_status, to_status=order.status, operator=request.user, reason='手动确认支付')
    return Response({'message': '支付确认成功', 'order_no': order_no, 'status': order.status})


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
