from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.content_security import SCENE_SOCIAL, ensure_texts_safe, user_openid
from apps.orders.models import Order
from apps.players.models import Player

from .models import ChatMessage, ChatReadStatus, KeywordAlert, KeywordAlertLog
from .serializers import ChatMessageSerializer, ChatReadStatusSerializer, ChatSendSerializer


def _order_queryset():
    return Order.objects.select_related(
        'boss_user',
        'boss_user__client_profile',
        'target_player',
        'target_player__user',
    ).prefetch_related('order_players__player__user')


def _display_name_for_user(user):
    profile = getattr(user, 'client_profile', None)
    if profile and profile.nickname:
        return profile.nickname[:50]
    full_name = (user.get_full_name() or '').strip() if hasattr(user, 'get_full_name') else ''
    return (full_name or getattr(user, 'username', '') or f'用户{user.pk}')[:50]


def _player_for_user(user):
    legacy_player = getattr(user, 'legacy_player', None)
    if legacy_player:
        return legacy_player
    return Player.objects.filter(user=user).first()


def _chat_actor(order, user):
    """Return the authenticated order participant represented in chat.

    The client is never trusted to choose sender/reader identity.  Bosses can
    access their own orders, players can access orders they accepted (or the
    targeted order assigned to them), and staff can access for support/audit.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return None

    if getattr(user, 'is_staff', False):
        return {
            'type': 'admin',
            'id': str(user.pk),
            'name': _display_name_for_user(user),
            'user': user,
        }

    if order.boss_user_id == user.pk:
        return {
            'type': 'boss',
            'id': str(user.pk),
            'name': _display_name_for_user(user),
            'user': user,
        }

    player = _player_for_user(user)
    if player and (
        order.target_player_id == player.pk
        or order.order_players.filter(player_id=player.pk).exists()
    ):
        return {
            'type': 'player',
            'id': str(player.pk),
            'name': player.name[:50],
            'user': player.user or user,
        }
    return None


def _forbidden():
    return Response({'detail': '无权访问该订单聊天'}, status=status.HTTP_403_FORBIDDEN)


def _chat_sender_openid(order, actor):
    openid = user_openid(actor.get('user'))
    if openid:
        return openid
    if order.boss_user_id:
        openid = user_openid(order.boss_user)
        if openid:
            return openid
    # 老数据可能只有 boss_wechat；当前订单契约中这里保存老板 OpenID。
    return str(order.boss_wechat or '')


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def send(request, order_no):
    order = _order_queryset().filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    actor = _chat_actor(order, request.user)
    if not actor:
        return _forbidden()

    # 保留旧前端仍会上传 sender_* 的兼容性，但服务端覆盖这些字段，客户端
    # 无法冒充老板/陪玩/管理员。
    incoming = request.data.copy()
    incoming['sender_type'] = actor['type']
    incoming['sender_id'] = actor['id']
    incoming['sender_name'] = actor['name']
    serializer = ChatSendSerializer(data=incoming)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    ensure_texts_safe(
        [data.get('sender_name'), data.get('content')],
        openid=_chat_sender_openid(order, actor),
        scene=SCENE_SOCIAL,
    )

    message = ChatMessage.objects.create(order=order, **data)
    for alert in KeywordAlert.objects.filter(is_active=True):
        if alert.keyword and alert.keyword in message.content:
            KeywordAlertLog.objects.create(
                keyword=alert,
                order=order,
                message=message,
                sender_name=message.sender_name,
                matched_keyword=alert.keyword,
                content_snippet=message.content[:200],
            )
    return Response(ChatMessageSerializer(message).data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def messages(request, order_no):
    order = _order_queryset().filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    if not _chat_actor(order, request.user):
        return _forbidden()
    qs = order.chat_messages.all()
    return Response(ChatMessageSerializer(qs, many=True).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def read(request, order_no):
    order = _order_queryset().filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    actor = _chat_actor(order, request.user)
    if not actor:
        return _forbidden()

    item, _ = ChatReadStatus.objects.update_or_create(
        order=order,
        reader_type=actor['type'],
        reader_id=actor['id'],
        defaults={'last_read_at': timezone.now()},
    )
    return Response(ChatReadStatusSerializer(item).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def read_status(request, order_no):
    order = _order_queryset().filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    if not _chat_actor(order, request.user):
        return _forbidden()
    return Response(ChatReadStatusSerializer(order.chat_read_statuses.all(), many=True).data)
