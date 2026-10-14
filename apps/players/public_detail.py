"""Read-only public detail contract; compatible with pre-archive releases."""
from django.conf import settings
from django.db.models import Q, Exists, OuterRef
from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from apps.accounts.models import ClientProfile
from apps.orders.models import Order
from .models import Player


def public_player_payload(player):
    profile = getattr(player.user, 'client_profile', None) if player.user else None
    billing_type = player.minimum_designated_player_type or player.player_type
    return {
    'id': player.id,
    'name': player.name,
    'type_id': player.player_type_id,
    'type_name': player.player_type.name if player.player_type else '',
    'type_priority': player.player_type.priority if player.player_type else 0,
    'price_extra': player.player_type.price_extra if player.player_type else 0,
    'designated_billing_type_id': billing_type.id if billing_type else None,
    'designated_billing_type_name': billing_type.name if billing_type else '',
    'designated_billing_type_priority': billing_type.priority if billing_type else 0,
    'avatar_url': profile.avatar_url if profile else None,
    'bio': player.bio,
    'audio_intro_url': player.audio_intro_url,
    'audio_intro_title': player.audio_intro_title,
    'player_type': {
        'id': player.player_type.id,
        'name': player.player_type.name,
        'priority': player.player_type.priority,
        'price_extra': player.player_type.price_extra or 0,
    } if player.player_type else None,
    'designated_billing_type': {
        'id': billing_type.id,
        'name': billing_type.name,
        'priority': billing_type.priority,
    } if billing_type else None,
    'avg_rating': round(player.avg_rating, 1) if player.avg_rating else 0,
    'rating_count': player.rating_count or 0,
    'total_orders': player.total_orders or 0,
    'is_online': player.is_online,
    'presence_online': player.presence_online,
    'can_be_designated': False if settings.FEATURE_DESIGNATE_DISABLED else player.can_be_designated,
    'status': '接单中' if player.has_active_order else ('在线' if player.is_online else '离线'),
    'created_at': player.created_at.isoformat() if player.created_at else None,
    }


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def public_detail(request, player_id):
    # No authentication/presence refresh: this is an anonymous public read.
    player = Player.objects.select_related(
        'player_type', 'minimum_designated_player_type', 'user__client_profile',
    ).annotate(has_active_order=Exists(Order.objects.filter(
        order_players__player=OuterRef('pk'), status=Order.STATUS_IN_PROGRESS,
    ))).filter(pk=player_id).first()
    unavailable = {'code': 'PLAYER_UNAVAILABLE', 'detail': '陪玩师不存在或已下架'}
    if player is None:
        return Response(unavailable, status=404)
    # Preserve the development tree's archive semantics without requiring its schema.
    if getattr(player, 'is_archived', False):
        return Response({'id': player.pk, 'name': player.archived_name or player.name,
            'is_archived': True, 'status': '已离开', 'is_online': False, 'presence_online': False,
            'can_be_designated': False, 'can_accept_orders': False})
    if player.status != Player.STATUS_APPROVED or not player.is_publicly_visible:
        return Response(unavailable, status=404)
    profile = getattr(player.user, 'client_profile', None) if player.user else None
    if player.user and not player.user.is_active:
        return Response(unavailable, status=404)
    if profile and profile.account_status != ClientProfile.ACCOUNT_STATUS_ACTIVE:
        expired = (profile.account_status == ClientProfile.ACCOUNT_STATUS_SUSPENDED
            and profile.account_suspended_until is not None
            and profile.account_suspended_until <= timezone.now())
        if not expired:
            return Response(unavailable, status=404)
    return Response(public_player_payload(player))
