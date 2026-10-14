"""One public read of current naming rights, without purchase or account writes."""
from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import CrownGrant
from .pricing import player_is_available
from .serializers import CrownSerializer


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def wall(request):
    now = timezone.now()
    grants = CrownGrant.objects.filter(
        purchase__payment_status='paid',
        starts_at__lte=now, expires_at__gt=now, revoked_at__isnull=True,
    ).select_related(
        'purchase__player__player_type',
        'purchase__player__user__client_profile',
        'purchase__boss__client_profile',
    ).order_by('-expires_at', '-id')

    by_player = {}
    for grant in grants:
        player = grant.purchase.player
        if not player_is_available(player):
            continue
        if player.pk not in by_player:
            by_player[player.pk] = (player, {})
        # Renewal receipts remain distinct for audit. The longest current right
        # per boss is displayed; distinct bosses still coexist on each player.
        by_player[player.pk][1].setdefault(grant.purchase.boss_id, grant)

    # Use the unrounded mean, as the public list does. Equal means use the
    # player ID for a stable display order; these rows do not imply a rank.
    ordered = sorted(by_player.values(), key=lambda row: (
        -(row[0].total_rating / row[0].rating_count if row[0].rating_count > 0 else 0),
        row[0].pk,
    ))
    results = []
    for player, bosses in ordered:
        profile = getattr(player.user, 'client_profile', None) if player.user_id else None
        results.append({
            'player': {
                'id': player.pk,
                'name': player.name,
                'avatar_url': profile.avatar_url if profile else '',
                'type_name': player.player_type.name if player.player_type else '',
            },
            'crowns': CrownSerializer(list(bosses.values()), many=True).data,
        })
    response = Response({'contract_version': '1.0', 'count': len(results), 'results': results})
    response['Cache-Control'] = 'no-store'
    return response
