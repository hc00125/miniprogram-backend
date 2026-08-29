from collections import Counter

from django.db.models import Q
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.common.permissions import IsApprovedPlayer, current_player
from apps.orders.cancellation_models import OrderReplacementState, PlayerCancellationRecord
from apps.orders.designations import pending_designation, pending_designation_count
from apps.orders.discipline import discipline_block_reason
from apps.orders.matching import expire_public_matching_order, expire_public_matching_orders
from apps.orders.models import Order, OrderStatusLog
from apps.orders.replacement_fixes import open_public_replacement
from apps.orders.serializers import AvailableOrderSerializer, OrderActionSerializer
from apps.orders.services import can_player_grab_order, grab_order as grab_order_service, remaining_type_slots

from .escort_qualification import escort_order_block_reason
from .views import django_operator


def candidate_public_orders():
    # 定时任务是2小时硬超时的主执行器；大厅查询再做一次轻量兜底，避免
    # scheduler 偶尔延迟时已到期订单仍短暂展示给陪玩。
    expire_public_matching_orders(limit=100)
    replacement_order_ids = OrderReplacementState.objects.filter(
        mode=OrderReplacementState.MODE_PUBLIC,
        status=OrderReplacementState.STATUS_OPEN,
        order__paid=True,
    ).values_list('order_id', flat=True)
    return (
        Order.objects
        .filter(
            Q(status=Order.STATUS_WAITING) | Q(id__in=replacement_order_ids),
            order_type=Order.ORDER_TYPE_NORMAL,
            fulfillment_mode=Order.FULFILLMENT_MODE_PUBLIC,
        )
        .select_related('package', 'addon')
        .prefetch_related('order_players__player__player_type', 'designations')
        .distinct()
    )


def hidden_order_reason(order, player):
    if order.order_players.filter(player=player).exists():
        return 'already_joined'
    if PlayerCancellationRecord.objects.filter(order=order, player=player).exists():
        return 'cancelled_before'
    if pending_designation(order, player):
        return 'pending_designation'
    if OrderStatusLog.objects.filter(
        order=order,
        operator_id=getattr(player, 'user_id', None),
        reason__startswith='陪玩进入房间超时',
    ).exists():
        return 'room_timeout'
    if escort_order_block_reason(order, player):
        return 'escort_required'

    replacement_state = open_public_replacement(order)
    if replacement_state and replacement_state.required_player_type_id:
        required_priority = 0
        try:
            required_priority = int(replacement_state.latest_cancellation.player.player_type.priority or 0)
        except (AttributeError, TypeError, ValueError):
            required_priority = 0
        player_priority = int(getattr(getattr(player, 'player_type', None), 'priority', 0) or 0)
        if player_priority < required_priority:
            return 'player_type'

    current_players = order.order_players.count()
    if current_players >= order.required_players:
        return 'full'

    pending_count = pending_designation_count(order)
    type_slots = remaining_type_slots(order)
    remaining_type_count = sum(int(item.get('count') or 0) for item in type_slots)
    public_slots = max(0, order.required_players - current_players - pending_count - remaining_type_count)
    player_priority = int(getattr(getattr(player, 'player_type', None), 'priority', 0) or 0)
    eligible_type_slot = False
    for item in type_slots:
        type_id = item.get('type_id')
        if not type_id:
            continue
        from apps.catalog.models import PlayerType
        required_type = PlayerType.objects.filter(id=type_id).first()
        if required_type and player_priority >= int(required_type.priority or 0):
            eligible_type_slot = True
            break
    if type_slots and not eligible_type_slot and public_slots <= 0:
        return 'player_type'
    if public_slots <= 0 and not eligible_type_slot:
        return 'reserved'
    return 'other'


def visibility_message(available_count, hidden_counts, total_count):
    if available_count:
        return f'当前有 {available_count} 个符合条件的公开订单'
    if not total_count:
        return '当前确实没有公开待接订单'
    priorities = [
        ('already_joined', '当前公开订单你已经接过，请到“我的订单”查看'),
        ('player_type', '有公开订单，但你的陪玩类型或等级不符合剩余名额要求'),
        ('escort_required', '有公开订单，但该订单要求已通过护航资格'),
        ('pending_designation', '该订单为老板给你的指定邀请，请在指定邀请区域处理'),
        ('cancelled_before', '有公开订单，但你已经取消过该单，不能重新抢回'),
        ('room_timeout', '有公开订单，但你曾在该单进房超时，不能重新抢回'),
        ('reserved', '有待接订单，但当前剩余名额已被指定邀请预留'),
        ('full', '订单名额刚刚已满，请刷新状态'),
    ]
    for key, message in priorities:
        if hidden_counts.get(key):
            return message
    return '有待接订单，但当前账号暂无可接名额'


@api_view(['POST'])
@permission_classes([IsApprovedPlayer])
def update_online_status(request):
    player = current_player(request.user)
    is_online = request.data.get('is_online')
    if not isinstance(is_online, bool):
        return Response({'detail': 'is_online 必须为布尔值 true/false'}, status=status.HTTP_400_BAD_REQUEST)
    block_reason = discipline_block_reason(player)
    if is_online and (not player.can_accept_orders or block_reason):
        return Response({'detail': block_reason or '管理员已暂停您的接单权限'}, status=status.HTTP_403_FORBIDDEN)
    player.is_online = is_online
    player.save(update_fields=['is_online', 'updated_at'])
    return Response({
        'id': player.id,
        'name': player.name,
        'is_online': player.is_online,
        'status': '在线' if player.is_online else '离线',
    })


@api_view(['GET'])
@permission_classes([IsApprovedPlayer])
def available_orders(request):
    player = current_player(request.user)
    if not player.can_accept_orders or discipline_block_reason(player):
        return Response([])
    result = []
    for order in candidate_public_orders():
        if escort_order_block_reason(order, player):
            continue
        order.can_player_grab = can_player_grab_order(order, player)
        if order.can_player_grab:
            result.append(order)
    return Response(AvailableOrderSerializer(result, many=True, context={'player': player}).data)


@api_view(['GET'])
@permission_classes([IsApprovedPlayer])
def available_orders_summary(request):
    player = current_player(request.user)
    block_reason = discipline_block_reason(player)
    if not player.can_accept_orders or block_reason:
        return Response({
            'available_count': 0,
            'public_order_count': 0,
            'hidden_counts': {'account_blocked': 1},
            'message': block_reason or '管理员已暂停您的接单权限',
        })

    orders = list(candidate_public_orders())
    hidden_counts = Counter()
    available_count = 0
    for order in orders:
        if can_player_grab_order(order, player) and not escort_order_block_reason(order, player):
            available_count += 1
        else:
            hidden_counts[hidden_order_reason(order, player)] += 1
    return Response({
        'available_count': available_count,
        'public_order_count': len(orders),
        'hidden_counts': dict(hidden_counts),
        'message': visibility_message(available_count, hidden_counts, len(orders)),
    })


@api_view(['POST'])
@permission_classes([IsApprovedPlayer])
def grab(request):
    player = current_player(request.user)
    block_reason = discipline_block_reason(player)
    if not player.can_accept_orders or block_reason:
        return Response({'detail': block_reason or '管理员已暂停您的接单权限'}, status=status.HTTP_403_FORBIDDEN)
    serializer = OrderActionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    order_no = serializer.validated_data['order_no']
    candidate = Order.objects.filter(order_no=order_no).first()
    if candidate and expire_public_matching_order(candidate):
        return Response({'detail': '该订单公开匹配已超过2小时并自动取消'}, status=status.HTTP_409_CONFLICT)
    try:
        order = grab_order_service(
            order_no,
            player,
            django_operator(request.user),
        )
    except Order.DoesNotExist:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    return Response({'message': '接单成功', 'order_no': order.order_no, 'status': order.status})
