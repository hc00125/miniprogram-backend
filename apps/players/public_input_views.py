import uuid

from django.core.files.storage import default_storage
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.content_security import (
    MEDIA_TYPE_AUDIO,
    SCENE_PROFILE,
    SCENE_SOCIAL,
    ensure_media_check_submitted,
    ensure_text_safe,
    user_openid,
)
from apps.common.permissions import IsApprovedPlayer, current_player
from apps.orders.models import Order
from apps.orders.serializers import OrderKookRoomSerializer


def _absolute_media_url(request, path):
    media_url = default_storage.url(path)
    if not media_url.startswith(('http://', 'https://', '/')):
        media_url = f'/{media_url}'
    return request.build_absolute_uri(media_url)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def upload_application_audio(request):
    """上传陪玩语音并提交微信异步媒体安全检测。

    语音并不会因上传而直接公开；它仍需随后随陪玩申请/资料修改进入人工审核。
    """
    file_obj = request.FILES.get('file')
    if not file_obj:
        return Response({'detail': '缺少音频文件'}, status=status.HTTP_400_BAD_REQUEST)

    allowed_types = {
        'audio/mpeg': 'mp3',
        'audio/mp3': 'mp3',
        'audio/mp4': 'm4a',
        'audio/x-m4a': 'm4a',
        'audio/aac': 'aac',
        'audio/wav': 'wav',
        'audio/x-wav': 'wav',
    }
    extension = allowed_types.get(file_obj.content_type)
    if not extension:
        original_name = (getattr(file_obj, 'name', '') or '').lower()
        if original_name.endswith('.mp3'):
            extension = 'mp3'
        elif original_name.endswith('.m4a'):
            extension = 'm4a'
        elif original_name.endswith('.aac'):
            extension = 'aac'
        elif original_name.endswith('.wav'):
            extension = 'wav'
    if not extension:
        return Response({'detail': '只支持 MP3/M4A/AAC/WAV 音频'}, status=status.HTTP_400_BAD_REQUEST)

    if file_obj.size > 20 * 1024 * 1024:
        return Response({'detail': '音频文件不能超过 20MB'}, status=status.HTTP_400_BAD_REQUEST)

    title = str(request.data.get('title') or getattr(file_obj, 'name', '') or '音频自我介绍').strip()
    ensure_text_safe(title, openid=user_openid(request.user), scene=SCENE_PROFILE)

    path = default_storage.save(f'player-audio/{request.user.id}_{uuid.uuid4().hex}.{extension}', file_obj)
    audio_url = _absolute_media_url(request, path)
    try:
        media_check = ensure_media_check_submitted(
            audio_url,
            openid=user_openid(request.user),
            media_type=MEDIA_TYPE_AUDIO,
            scene=SCENE_PROFILE,
        )
    except Exception:
        # 微信未接受安全检测任务时，不保留这次新上传的媒体。
        default_storage.delete(path)
        raise

    return Response({
        'audio_intro_url': audio_url,
        'audio_intro_title': title,
        'content_security_trace_id': (media_check or {}).get('trace_id', ''),
        'content_security_status': 'submitted' if media_check else 'disabled_in_development',
    })


@api_view(['POST'])
@permission_classes([IsApprovedPlayer])
def set_kook_room(request, order_no):
    player = current_player(request.user)
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    if not order.order_players.filter(player=player).exists():
        return Response({'detail': '您不是这个订单的陪玩师'}, status=status.HTTP_403_FORBIDDEN)
    if order.status in {Order.STATUS_COMPLETED, Order.STATUS_CANCELLED}:
        return Response({'detail': '当前订单状态不能填写 KOOK 房间号'}, status=status.HTTP_400_BAD_REQUEST)

    serializer = OrderKookRoomSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    room_number = serializer.validated_data['kook_room_number']
    ensure_text_safe(room_number, openid=user_openid(request.user), scene=SCENE_SOCIAL)

    order.kook_room_number = room_number
    order.kook_room_updated_at = timezone.now()
    order.kook_room_updated_by = request.user if getattr(request.user, 'is_authenticated', False) else None
    order.save(update_fields=['kook_room_number', 'kook_room_updated_at', 'kook_room_updated_by'])
    return Response({
        'message': 'KOOK 房间号已保存',
        'order_no': order.order_no,
        'kook_room_number': order.kook_room_number,
        'kook_room_updated_at': order.kook_room_updated_at,
    })
