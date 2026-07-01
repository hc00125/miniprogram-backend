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
