import json
import uuid
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.decorators import parser_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from apps.common.content_security import SCENE_PROFILE, ensure_image_safe, ensure_text_safe
from apps.payments.virtualpay import get_access_token
from apps.players.models import Player

from .models import ClientProfile
from .nicknames import get_or_create_wechat_profile
from .phone_models import ClientPhoneBinding
from .serializers import ClientProfileSerializer, WechatLoginSerializer


def issue_token(user):
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token)


def allow_dev_openid_login():
    return bool(settings.DEBUG or settings.ENABLE_DEV_OPENID_LOGIN)


def resolve_openid(code='', openid=''):
    if openid:
        if allow_dev_openid_login():
            return openid, ''
        raise ValueError('生产环境不允许直接传 openid 登录，请使用 wx.login code')
    if settings.WECHAT_APP_ID and settings.WECHAT_APP_SECRET:
        params = urlencode({
            'appid': settings.WECHAT_APP_ID,
            'secret': settings.WECHAT_APP_SECRET,
            'js_code': code,
            'grant_type': 'authorization_code',
        })
        endpoint = 'https://' + 'api.weixin.qq.com' + '/sns/jscode2session'
        with urlopen(f'{endpoint}?{params}', timeout=8) as response:
            data = json.loads(response.read().decode('utf-8'))
        if not data.get('openid'):
            raise ValueError(data.get('errmsg') or '微信登录失败')
        return data['openid'], data.get('unionid') or ''
    raise ValueError('微信登录配置不完整，请联系管理员')


def resolve_phone_number(code):
    if not code:
        raise ValueError('缺少手机号授权 code，请重新点击绑定手机号')
    token = get_access_token()
    query = urlencode({'access_token': token})
    request = Request(
        f'https://api.weixin.qq.com/wxa/business/getuserphonenumber?{query}',
        data=json.dumps({'code': code}, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        method='POST',
    )
    with urlopen(request, timeout=settings.WECHAT_PHONE_NUMBER_HTTP_TIMEOUT) as response:
        data = json.loads(response.read().decode('utf-8'))
    if int(data.get('errcode') or 0) != 0:
        raise ValueError(data.get('errmsg') or '微信手机号验证失败')

    phone_info = data.get('phone_info') or {}
    phone_number = str(phone_info.get('purePhoneNumber') or phone_info.get('phoneNumber') or '').strip()
    country_code = str(phone_info.get('countryCode') or '86').strip() or '86'
    if phone_number.startswith(f'+{country_code}'):
        phone_number = phone_number[len(country_code) + 1:]
    elif country_code == '86' and phone_number.startswith('86') and len(phone_number) > 11:
        phone_number = phone_number[2:]
    if not phone_number:
        raise ValueError('微信未返回可用手机号，请重新授权')
    return phone_number, country_code


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

    try:
        profile, created = get_or_create_wechat_profile(user, openid)
    except RuntimeError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    profile.openid = openid
    profile.unionid = unionid or profile.unionid

    if created:
        # 新用户默认昵称由 OpenID 的不可逆摘要生成，例如“微信用户-4A8F2D91C7”。
        profile.nickname_customized = False
    else:
        # 客户端带回的微信昵称仍属于可展示用户内容，保存前必须经过内容安全检测。
        wx_nickname = serializer.validated_data.get('nickname')
        if wx_nickname and wx_nickname != '微信用户' and not profile.nickname_customized:
            ensure_text_safe(wx_nickname, openid=openid, scene=SCENE_PROFILE)
            profile.nickname = wx_nickname

    # 不再信任登录请求直接携带的 avatar_url。头像只能经过 /profile/avatar
    # 上传并完成图片内容安全检测后才能成为公开头像，避免绕过审核。
    profile.save()
    return Response({'token': issue_token(user), 'profile': ClientProfileSerializer(profile).data})


@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated])
def profile(request):
    profile_obj = getattr(request.user, 'client_profile', None)
    if not profile_obj:
        return Response({'detail': '请先微信登录'}, status=status.HTTP_404_NOT_FOUND)
    if request.method == 'PUT':
        nickname_provided = 'nickname' in request.data
        avatar_provided = 'avatar_url' in request.data
        player_obj = getattr(request.user, 'player_profile', None)

        if nickname_provided:
            nickname = (request.data.get('nickname') or '').strip()
            if not nickname:
                return Response({'detail': '昵称不能为空'}, status=status.HTTP_400_BAD_REQUEST)
            if len(nickname) > ClientProfile._meta.get_field('nickname').max_length:
                return Response({'detail': '昵称过长'}, status=status.HTTP_400_BAD_REQUEST)
            if player_obj and len(nickname) > Player._meta.get_field('name').max_length:
                return Response({'detail': '陪玩师昵称不能超过50个字符'}, status=status.HTTP_400_BAD_REQUEST)
            if ClientProfile.objects.filter(nickname=nickname).exclude(pk=profile_obj.pk).exists():
                return Response({'detail': '昵称已被使用'}, status=status.HTTP_400_BAD_REQUEST)
            if Player.objects.filter(name=nickname).exclude(user=request.user).exists():
                return Response({'detail': '昵称已被陪玩师使用'}, status=status.HTTP_400_BAD_REQUEST)
            ensure_text_safe(nickname, openid=profile_obj.openid, scene=SCENE_PROFILE)

        requested_avatar = str(request.data.get('avatar_url') or '') if avatar_provided else None
        if avatar_provided and requested_avatar and requested_avatar != (profile_obj.avatar_url or ''):
            return Response(
                {'detail': '头像必须通过头像上传接口完成安全检测后保存'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                profile_update_fields = []
                if nickname_provided:
                    profile_obj.nickname = nickname
                    profile_obj.nickname_customized = True
                    profile_update_fields.extend(['nickname', 'nickname_customized'])
                # 允许清空头像；新的非空头像只能由 avatar 上传接口写入。
                if avatar_provided and requested_avatar == '' and profile_obj.avatar_url:
                    profile_obj.avatar_url = ''
                    profile_update_fields.append('avatar_url')
                if profile_update_fields:
                    profile_obj.save(update_fields=[*profile_update_fields, 'updated_at'])
                if nickname_provided and player_obj and player_obj.name != nickname:
                    player_obj.name = nickname
                    player_obj.save(update_fields=['name', 'updated_at'])
        except IntegrityError:
            return Response({'detail': '昵称已被使用'}, status=status.HTTP_400_BAD_REQUEST)
    return Response(ClientProfileSerializer(profile_obj).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def bind_phone_number(request):
    profile_obj = getattr(request.user, 'client_profile', None)
    if not profile_obj:
        return Response({'detail': '请先微信登录'}, status=status.HTTP_404_NOT_FOUND)

    code = str(request.data.get('code') or '').strip()
    try:
        phone_number, country_code = resolve_phone_number(code)
    except ValueError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        return Response({'detail': '微信手机号服务暂不可用，请稍后重试'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    ClientPhoneBinding.objects.update_or_create(
        profile=profile_obj,
        defaults={
            'phone_number': phone_number,
            'country_code': country_code,
        },
    )
    profile_obj.refresh_from_db()
    return Response({
        'detail': '手机号绑定成功',
        'profile': ClientProfileSerializer(profile_obj).data,
    })


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

    # 先审核、后落盘、后公开。违规图片永远不会成为公开头像。
    ensure_image_safe(file_obj, openid=profile_obj.openid)

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
