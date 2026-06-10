from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from apps.players.models import Player


class LegacyPlayerTokenAuthentication(BaseAuthentication):
    def authenticate(self, request):
        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            return None
        token = header[7:].strip()
        if not token:
            return None
        player = Player.objects.select_related('user').filter(session_token=token).first()
        if not player:
            return None
        if player.token_expires_at and player.token_expires_at < timezone.now():
            raise AuthenticationFailed('登录已过期，请重新登录')
        user = player.user
        if user is None:
            return (LegacyPlayerUser(player), token)
        user.legacy_player = player
        return (user, token)


class LegacyPlayerUser:
    is_authenticated = True
    is_staff = False
    is_superuser = False

    def __init__(self, player):
        self.legacy_player = player
        self.id = None
        self.pk = None
        self.username = player.name

    def __str__(self):
        return self.username
