"""Legacy token lookup without the shared authenticator's user-creation repair."""
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from apps.players.models import Player


class ReadOnlyLegacyAuthentication(BaseAuthentication):
    def authenticate_header(self, request):
        return 'Bearer'

    def authenticate(self, request):
        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            return None
        token = header[7:].strip()
        if not token:
            return None
        player = Player.objects.select_related('user').filter(session_token=token).first()
        if player is None:
            return None
        if not player.user_id or not player.user.is_active or not player.token_expires_at or player.token_expires_at <= timezone.now():
            raise AuthenticationFailed('请重新登录')
        return player.user, token
