"""Recent mini-program activity, deliberately independent of order availability."""
from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import current_player
from .models import Player


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def heartbeat(request):
    player = current_player(request.user)
    if not player or player.status != Player.STATUS_APPROVED:
        return Response({'tracked': False})
    now = timezone.now()
    # Update only this caller's timestamp; never toggle rest/discipline/admin flags.
    # Conditional update prevents a delayed concurrent request regressing the clock.
    Player.objects.filter(pk=player.pk).filter(
        Q(presence_seen_at__isnull=True) | Q(presence_seen_at__lt=now)
    ).update(presence_seen_at=now)
    return Response({'tracked': True, 'player_id': player.pk, 'presence_online': True})
