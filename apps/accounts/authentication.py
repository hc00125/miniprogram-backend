from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from apps.players.models import Player


class LegacyPlayerTokenAuthentication(BaseAuthentication):
    def authenticate(self, request):
        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            return None
        session_key = header[7:].strip()
        if not session_key:
            return None
        player = Player.objects.select_related('user').filter(session_token=session_key).first()
        if not player:
            return None
        if player.token_expires_at and player.token_expires_at < timezone.now():
            raise AuthenticationFailed('登录已过期，请重新登录')
        user = player.user or self.ensure_user_for_player(player)
        user.legacy_player = player
        return (user, session_key)

    @transaction.atomic
    def ensure_user_for_player(self, player):
        if player.user_id:
            return player.user
        user_model = get_user_model()
        username = f'legacy_player_{player.id}'
        user, _created = user_model.objects.get_or_create(
            username=username,
            defaults={
                'first_name': player.name[:150],
                'is_active': True,
            },
        )
        if player.user_id != user.id:
            player.user = user
            player.save(update_fields=['user', 'updated_at'])
        return user


class LenientJWTAuthentication(BaseAuthentication):
    """宽容版 JWT 认证：令牌无效/过期时返回 None 而不是抛异常。

    标准版 JWTAuthentication 会在令牌无效时直接抛 AuthenticationFailed，
    导致 AllowAny 的接口也返回 403。这个版本静默失败，让上层权限类自己决定是否阻止访问。
    """

    def authenticate(self, request):
        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            return None
        token_str = header[7:].strip()
        if not token_str:
            return None

        from rest_framework_simplejwt.authentication import JWTAuthentication
        from rest_framework_simplejwt.exceptions import TokenError

        auth = JWTAuthentication()
        try:
            validated_token = auth.get_validated_token(token_str)
            user = auth.get_user(validated_token)
            return (user, validated_token)
        except (TokenError, AuthenticationFailed):
            # 令牌无效/过期 → 静默跳过，不阻止 AllowAny 视图
            return None
