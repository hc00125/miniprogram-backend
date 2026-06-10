from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType
from apps.common.permissions import IsApprovedPlayer, current_player
from apps.common.tokens import generate_session_token
from apps.orders.models import Order
from apps.orders.serializers import AvailableOrderSerializer, OrderActionSerializer, PlayerOrderDetailSerializer, PlayerOrderListSerializer
from apps.orders.services import can_player_grab_order, complete_order as complete_order_service, grab_order as grab_order_service, pause_order, resume_order, start_timer
from .models import Player, PlayerApplication
from .serializers import PlayerApplicationCreateSerializer, PlayerApplicationSerializer, PlayerLoginSerializer, PlayerSerializer


def django_operator(user):
    return user if isinstance(user, User) else None


@api_view(['POST'])
@permission_classes([AllowAny])
def login(request):
    serializer = PlayerLoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    player_type = PlayerType.objects.filter(id=serializer.validated_data['type_id']).first()
    if not player_type:
        return Response({'detail': '打手类型不存在'}, status=status.HTTP_400_BAD_REQUEST)
    player, created = Player.objects.get_or_create(
        name=serializer.validated_data['name'],
        defaults={'player_type': player_type, 'status': Player.STATUS_APPROVED},
    )
    if created is False and not player.player_type_id:
        player.player_type = player_type
    token = generate_session_token()
    player.refresh_legacy_token(token)
    player.save()
    return Response({
        'token': token,
        'player': PlayerSerializer(player).data,
        'expires_at': player.token_expires_at.isoformat(),
    })


@api_view(['POST'])
@permission_classes([IsApprovedPlayer])
def logout(request):
    player = current_player(request.user)
    player.is_online = False
    player.session_token = None
    player.save(update_fields=['is_online', 'session_token'])
    return Response({'message': '已退出登录'})


@api_view(['GET'])
@permission_classes([IsApprovedPlayer])
def me(request):
    return Response(PlayerSerializer(current_player(request.user)).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def apply(request):
    serializer = PlayerApplicationCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    latest = PlayerApplication.objects.filter(user=request.user).order_by('-submitted_at').first()
    if latest and latest.status == PlayerApplication.STATUS_PENDING:
        return Response({'detail': '陪玩师申请审核中'}, status=status.HTTP_400_BAD_REQUEST)
    application = serializer.save(user=request.user, status=PlayerApplication.STATUS_PENDING)
    profile = getattr(request.user, 'client_profile', None)
    if profile:
        profile.player_status = ClientProfile.PLAYER_STATUS_PENDING
        profile.save(update_fields=['player_status', 'updated_at'])
    return Response(PlayerApplicationSerializer(application).data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def apply_status(request):
    application = PlayerApplication.objects.filter(user=request.user).order_by('-submitted_at').first()
    profile = getattr(request.user, 'client_profile', None)
    return Response({
        'player_status': profile.player_status if profile else 'none',
        'application': PlayerApplicationSerializer(application).data if application else None,
        'player': PlayerSerializer(getattr(request.user, 'player_profile', None)).data if hasattr(request.user, 'player_profile') else None,
    })


@api_view(['GET'])
@permission_classes([IsApprovedPlayer])
def available_orders(request):
    player = current_player(request.user)
    orders = Order.objects.filter(status=Order.STATUS_WAITING).select_related('package', 'addon').prefetch_related('order_players')
    result = []
    for order in orders:
        order.can_player_grab = can_player_grab_order(order, player)
        if order.can_player_grab:
            result.append(order)
    return Response(AvailableOrderSerializer(result, many=True, context={'player': player}).data)


@api_view(['POST'])
@permission_classes([IsApprovedPlayer])
def grab(request):
    serializer = OrderActionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        order = grab_order_service(serializer.validated_data['order_no'], current_player(request.user), django_operator(request.user))
    except Order.DoesNotExist:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    return Response({'message': '接单成功', 'order_no': order.order_no, 'status': order.status})


@api_view(['GET'])
@permission_classes([IsApprovedPlayer])
def my_orders(request):
    player = current_player(request.user)
    orders = Order.objects.filter(order_players__player=player).select_related('package', 'addon').order_by('-order_players__grab_time')
    return Response(PlayerOrderListSerializer(orders, many=True, context={'player': player}).data)


@api_view(['GET'])
@permission_classes([IsApprovedPlayer])
def order_detail(request, order_no):
    player = current_player(request.user)
    order = Order.objects.filter(order_no=order_no).select_related('package', 'addon').prefetch_related('order_players__player__player_type').first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    if not order.order_players.filter(player=player).exists():
        return Response({'detail': '您不是这个订单的打手'}, status=status.HTTP_403_FORBIDDEN)
    return Response(PlayerOrderDetailSerializer(order).data)


@api_view(['POST'])
@permission_classes([IsApprovedPlayer])
def start_timer_view(request):
    serializer = OrderActionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    order = Order.objects.filter(order_no=serializer.validated_data['order_no']).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    order = start_timer(order, current_player(request.user))
    return Response({'message': '计时已开始', 'timer_started_at': order.timer_started_at.isoformat()})


@api_view(['POST'])
@permission_classes([IsApprovedPlayer])
def complete(request):
    serializer = OrderActionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    order = Order.objects.filter(order_no=serializer.validated_data['order_no']).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    order = complete_order_service(order, current_player(request.user), django_operator(request.user))
    return Response({'message': '已标记完成', 'order_no': order.order_no, 'status': order.status})


@api_view(['POST'])
@permission_classes([IsApprovedPlayer])
def pause(request, order_no):
    player = current_player(request.user)
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    if not order.order_players.filter(player=player).exists():
        return Response({'detail': '您不是这个订单的打手'}, status=status.HTTP_403_FORBIDDEN)
    pause_order(order)
    return Response({'message': '计时已暂停'})


@api_view(['GET'])
@permission_classes([AllowAny])
def list(request):
    """获取陪玩师列表"""
    queryset = Player.objects.filter(status=Player.STATUS_APPROVED).select_related('player_type', 'user')
    
    # 按类型筛选
    type_id = request.query_params.get('type_id')
    if type_id:
        queryset = queryset.filter(player_type_id=type_id)
    
    # 按在线状态筛选
    is_online = request.query_params.get('is_online')
    if is_online is not None:
        queryset = queryset.filter(is_online=is_online.lower() == 'true')
    
    # 搜索名字
    search = request.query_params.get('search')
    if search:
        queryset = queryset.filter(name__icontains=search)
    
    # 排序
    ordering = request.query_params.get('ordering', '-avg_rating')
    if ordering in ['avg_rating', '-avg_rating', 'total_orders', '-total_orders', 'created_at', '-created_at']:
        if ordering == 'avg_rating':
            queryset = sorted(queryset, key=lambda p: p.avg_rating, reverse=False)
        elif ordering == '-avg_rating':
            queryset = sorted(queryset, key=lambda p: p.avg_rating, reverse=True)
        elif ordering == 'total_orders':
            queryset = queryset.order_by('total_orders')
        elif ordering == '-total_orders':
            queryset = queryset.order_by('-total_orders')
        elif ordering == 'created_at':
            queryset = queryset.order_by('created_at')
        elif ordering == '-created_at':
            queryset = queryset.order_by('-created_at')
        else:
            queryset = queryset.order_by('-total_rating')
    else:
        # 默认按评分排序（通过Python计算）
        queryset = sorted(queryset, key=lambda p: p.avg_rating, reverse=True)
    
    result = []
    for player in queryset:
        active_order = player.order_players.filter(order__status=Order.STATUS_IN_PROGRESS).exists()
        avatar_url = None
        if hasattr(player, 'user') and player.user:
            profile = getattr(player.user, 'client_profile', None)
            if profile:
                avatar_url = profile.avatar_url
        result.append({
            'id': player.id,
            'name': player.name,
            'avatar_url': avatar_url,
            'bio': player.bio,
            'player_type': {
                'id': player.player_type.id,
                'name': player.player_type.name,
                'price_extra': player.player_type.price_extra or 0,
            } if player.player_type else None,
            'avg_rating': round(player.avg_rating, 1) if player.avg_rating else 0,
            'total_orders': player.total_orders or 0,
            'is_online': player.is_online,
            'status': '接单中' if active_order else ('在线' if player.is_online else '离线'),
            'created_at': player.created_at.isoformat() if player.created_at else None,
        })
    return Response(result)


@api_view(['POST'])
@permission_classes([IsApprovedPlayer])
def resume(request, order_no):
    player = current_player(request.user)
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    if not order.order_players.filter(player=player).exists():
        return Response({'detail': '您不是这个订单的打手'}, status=status.HTTP_403_FORBIDDEN)
    order = resume_order(order)
    return Response({'message': '计时已继续', 'paused_duration': order.paused_duration})

