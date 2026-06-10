import hashlib
import json
import uuid
from urllib.parse import urlencode
from urllib.request import urlopen

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.decorators import parser_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .models import ClientProfile
from .serializers import ClientProfileSerializer, WechatLoginSerializer


def issue_token(user):
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token)


def resolve_openid(code='', openid=''):
    if openid:
        return openid, ''
    if settings.WECHAT_APP_ID and settings.WECHAT_APP_SECRET:
        params = urlencode({
            'appid': settings.WECHAT_APP_ID,
            'secret': settings.WECHAT_APP_SECRET,
            'js_code': code,
            'grant_type': 'authorization_code',
        })
        with urlopen(f'https://api.weixin.qq.com/sns/jscode2session?{params}', timeout=8) as response:
            data = json.loads(response.read().decode('utf-8'))
        if not data.get('openid'):
            raise ValueError(data.get('errmsg') or '微信登录失败')
        return data['openid'], data.get('unionid') or ''
    digest = hashlib.sha256(code.encode('utf-8')).hexdigest()[:32]
    return f'dev_{digest}', ''


def make_default_nickname():
    """生成唯一的默认昵称，如 微信用户001"""
    prefix = '微信用户'
    existing = (
        ClientProfile.objects
        .filter(nickname__startswith=prefix)
        .values_list('nickname', flat=True)
        .order_by('nickname')
    )
    used_numbers = set()
    for nick in existing:
        num_str = nick[len(prefix):]
        try:
            used_numbers.add(int(num_str))
        except ValueError:
            pass
    for i in range(1, 999999):
        if i not in used_numbers:
            return f'{prefix}{i:03d}'  # 微信用户001 ~ 微信用户999999
    return f'{prefix}{999999}'


@api_view(['POST'])
@permission_classes([AllowAny])
def wechat_login(request):
    serializer = WechatLoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        openid, unionid = resolve_openid(serializer.validated_data.get('code') or '', serializer.validated_data.get('openid') or '')
    except ValueError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    user, _ = User.objects.get_or_create(username=f'wx_{openid[:120]}')
    if not user.has_usable_password():
        user.set_unusable_password()
        user.save(update_fields=['password'])

    is_new = not hasattr(user, 'client_profile')
    profile, created = ClientProfile.objects.get_or_create(user=user, defaults={'openid': openid})
    profile.openid = openid
    profile.unionid = unionid or profile.unionid

    if is_new:
        # 新用户：生成唯一默认昵称
        profile.nickname = make_default_nickname()
        profile.nickname_customized = False
    else:
        # 老用户：只有从未自定义过昵称，且微信昵称不是默认占位符时，才覆盖
        wx_nickname = serializer.validated_data.get('nickname')
        if wx_nickname and wx_nickname != '微信用户' and not profile.nickname_customized:
            profile.nickname = wx_nickname

    if 'avatar_url' in serializer.validated_data:
        avatar = serializer.validated_data.get('avatar_url')
        if avatar:
            profile.avatar_url = avatar

    profile.save()
    return Response({'token': issue_token(user), 'profile': ClientProfileSerializer(profile).data})


@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated])
def profile(request):
    profile_obj = getattr(request.user, 'client_profile', None)
    if not profile_obj:
        return Response({'detail': '请先微信登录'}, status=status.HTTP_404_NOT_FOUND)
    if request.method == 'PUT':
        for field in ['nickname', 'avatar_url']:
            if field in request.data:
                setattr(profile_obj, field, request.data.get(field) or '')
        if 'nickname' in request.data:
            profile_obj.nickname_customized = True
        profile_obj.save(update_fields=['nickname', 'avatar_url', 'nickname_customized', 'updated_at'])
    return Response(ClientProfileSerializer(profile_obj).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def avatar(request):
    profile_obj = getattr(request.user, 'client_profile', None)
    if not profile_obj:
        return Response({'detail': '请先微信登录'}, status=status.HTTP_404_NOT_FOUND)

    file_obj = request.FILES.get('file')
    if not file_obj:
        return Response({'detail': '缺少头像文件'}, status=status.HTTP_400_BAD_REQUEST)

    allowed_types = {'image/jpeg', 'image/png', 'image/webp'}
    if file_obj.content_type not in allowed_types:
        return Response({'detail': '只支持 JPG/PNG/WEBP 图片'}, status=status.HTTP_400_BAD_REQUEST)

    if file_obj.size > 5 * 1024 * 1024:
        return Response({'detail': '头像文件不能超过 5MB'}, status=status.HTTP_400_BAD_REQUEST)

    extension = {
        'image/jpeg': 'jpg',
        'image/png': 'png',
        'image/webp': 'webp',
    }[file_obj.content_type]
    path = default_storage.save(f'avatars/{request.user.id}_{uuid.uuid4().hex}.{extension}', file_obj)
    media_url = default_storage.url(path)
    if not media_url.startswith(('http://', 'https://', '/')):
        media_url = f'/{media_url}'
    avatar_url = request.build_absolute_uri(media_url)

    profile_obj.avatar_url = avatar_url
    profile_obj.save(update_fields=['avatar_url', 'updated_at'])

    return Response({
        'avatar_url': avatar_url,
        'profile': ClientProfileSerializer(profile_obj).data,
    })
