from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.common.content_security import SCENE_SOCIAL, ensure_texts_safe, user_openid
from apps.orders.models import Order
from apps.players.models import Player

from .models import ChatMessage, ChatReadStatus, KeywordAlert, KeywordAlertLog
from .serializers import ChatMessageSerializer, ChatReadStatusSerializer, ChatSendSerializer


def _chat_sender_openid(order, data):
    sender_type = data.get('sender_type')
    sender_id = str(data.get('sender_id') or '').strip()

    if sender_type == 'player' and sender_id.isdigit():
        player = (
            Player.objects
            .filter(pk=int(sender_id))
            .select_related('user__client_profile')
            .first()
        )
        if player and player.user:
            openid = user_openid(player.user)
            if openid:
                return openid

    if order.boss_user_id:
        openid = user_openid(order.boss_user)
        if openid:
            return openid

    # 老数据可能只有 boss_wechat；当前订单契约中这里保存老板 OpenID。
    return str(order.boss_wechat or '')


@api_view(['POST'])
@permission_classes([AllowAny])
def send(request, order_no):
    order = Order.objects.filter(order_no=order_no).select_related('boss_user__client_profile').first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    serializer = ChatSendSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    # 聊天发送者名称和正文都会被其他人看到，发布前统一检测。
    ensure_texts_safe(
        [data.get('sender_name'), data.get('content')],
        openid=_chat_sender_openid(order, data),
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
@permission_classes([AllowAny])
def messages(request, order_no):
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    qs = order.chat_messages.all()
    return Response(ChatMessageSerializer(qs, many=True).data)


@api_view(['POST'])
@permission_classes([AllowAny])
def read(request, order_no):
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    reader_type = request.data.get('reader_type')
    reader_id = str(request.data.get('reader_id') or '')
    if not reader_type or not reader_id:
        return Response({'detail': '缺少 reader_type 或 reader_id'}, status=status.HTTP_400_BAD_REQUEST)
    item, _ = ChatReadStatus.objects.update_or_create(
        order=order,
        reader_type=reader_type,
        reader_id=reader_id,
        defaults={'last_read_at': timezone.now()},
    )
    return Response(ChatReadStatusSerializer(item).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def read_status(request, order_no):
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    return Response(ChatReadStatusSerializer(order.chat_read_statuses.all(), many=True).data)
