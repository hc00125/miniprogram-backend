from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import ClientProfile
from apps.common.content_security import SCENE_PROFILE, ensure_texts_safe, user_openid

from .models import Player, PlayerApplication
from .serializers import PlayerApplicationCreateSerializer, PlayerApplicationSerializer


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def apply(request):
    """提交陪玩师申请，并优先处理当前账号已有的申请/陪玩状态。"""
    profile = getattr(request.user, 'client_profile', None)
    player = getattr(request.user, 'player_profile', None)

    # 已经通过审核的账号不能再创建第二条待审核申请；资料修改应走陪玩资料修改流程。
    if (
        player and player.status == Player.STATUS_APPROVED
    ) or (
        profile and profile.player_status == ClientProfile.PLAYER_STATUS_APPROVED
    ):
        return Response(
            {
                'detail': '您已经是审核通过的陪玩师，无需重复申请',
                'code': 'PLAYER_ALREADY_APPROVED',
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    # 先判断本人是否已有待审核申请，避免字段级昵称校验抢先返回误导信息。
    latest = PlayerApplication.objects.filter(
        user=request.user,
    ).order_by('-submitted_at').first()
    if latest and latest.status == PlayerApplication.STATUS_PENDING:
        return Response(
            {
                'detail': '您的陪玩师申请正在审核中，请勿重复提交',
                'code': 'PLAYER_APPLICATION_PENDING',
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    serializer = PlayerApplicationCreateSerializer(
        data=request.data,
        context={'request': request},
    )
    serializer.is_valid(raise_exception=True)

    # 只检测会成为公开资料的 UGC。真实姓名和联系方式属于私密审核资料，
    # 不发送到内容安全接口。
    public_name = profile.nickname if profile else serializer.validated_data.get('name')
    ensure_texts_safe(
        [
            public_name,
            serializer.validated_data.get('bio'),
            serializer.validated_data.get('audio_intro_title'),
        ],
        openid=user_openid(request.user),
        scene=SCENE_PROFILE,
    )

    application = serializer.save(
        user=request.user,
        status=PlayerApplication.STATUS_PENDING,
    )

    if profile:
        profile.player_status = ClientProfile.PLAYER_STATUS_PENDING
        profile.save(update_fields=['player_status', 'updated_at'])

    return Response(
        PlayerApplicationSerializer(application).data,
        status=status.HTTP_201_CREATED,
    )
