from django.db import transaction
from django.db.models import Avg, Case, Count, Exists, ExpressionWrapper, F, FloatField, OuterRef, Q, Value, When
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.common.permissions import IsApprovedPlayer, current_player
from apps.orders.models import Order

from .models import Player, PlayerProfileUpdateRequest
from .serializers import (
    PlayerProfileUpdateCreateSerializer,
    PlayerProfileUpdateRequestSerializer,
    PlayerSerializer,
)


@api_view(['GET'])
@permission_classes([AllowAny])
def public_list(request):
    active_orders = Order.objects.filter(
        order_players__player=OuterRef('pk'),
        status=Order.STATUS_IN_PROGRESS,
    )
    queryset = (
        Player.objects
        .filter(status=Player.STATUS_APPROVED, is_publicly_visible=True)
        .select_related(
            'player_type', 'minimum_designated_player_type',
            'user', 'user__client_profile',
        )
        .annotate(
            has_active_order=Exists(active_orders),
            avg_rating_value=Case(
                When(
                    rating_count__gt=0,
                    then=ExpressionWrapper(F('total_rating') * 1.0 / F('rating_count'), output_field=FloatField()),
                ),
                default=Value(0.0),
                output_field=FloatField(),
            ),
        )
    )

    type_id = request.query_params.get('type_id')
    if type_id:
        queryset = queryset.filter(player_type_id=type_id)

    is_online = request.query_params.get('is_online')
    if is_online is not None:
        queryset = queryset.filter(is_online=is_online.lower() == 'true')

    search = (request.query_params.get('search') or '').strip()
    if search:
        queryset = queryset.filter(
            Q(name__icontains=search)
            | Q(bio__icontains=search)
            | Q(player_type__name__icontains=search)
            | Q(minimum_designated_player_type__name__icontains=search)
        )

    ordering = request.query_params.get('ordering', '-avg_rating')
    ordering_map = {
        'avg_rating': ('avg_rating_value', 'id'),
        '-avg_rating': ('-avg_rating_value', '-rating_count', '-total_orders', 'id'),
        'total_orders': ('total_orders', 'id'),
        '-total_orders': ('-total_orders', 'id'),
        'created_at': ('created_at', 'id'),
        '-created_at': ('-created_at', 'id'),
    }
    queryset = queryset.order_by(*ordering_map.get(ordering, ordering_map['-avg_rating']))

    result = []
    for player in queryset:
        profile = getattr(player.user, 'client_profile', None) if player.user else None
        billing_type = player.minimum_designated_player_type or player.player_type
        result.append({
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
            'can_be_designated': player.can_be_designated,
            'status': '接单中' if player.has_active_order else ('在线' if player.is_online else '离线'),
            'created_at': player.created_at.isoformat() if player.created_at else None,
        })
    return Response(result)


def profile_settings_payload(player):
    pending = (
        player.profile_update_requests
        .filter(status=PlayerProfileUpdateRequest.STATUS_PENDING)
        .order_by('-submitted_at')
        .first()
    )
    latest = player.profile_update_requests.order_by('-submitted_at').first()
    return {
        'player': PlayerSerializer(player).data,
        'permissions': {
            'can_accept_orders': player.can_accept_orders,
            'can_be_designated': player.can_be_designated,
            'is_publicly_visible': player.is_publicly_visible,
            'can_withdraw': player.can_withdraw,
        },
        'pending_update': PlayerProfileUpdateRequestSerializer(pending).data if pending else None,
        'latest_update': PlayerProfileUpdateRequestSerializer(latest).data if latest else None,
        'review_notice': '个人简介和语音修改提交后进入审核，审核期间继续展示旧资料。',
    }


@api_view(['GET', 'POST'])
@permission_classes([IsApprovedPlayer])
def profile_settings(request):
    player = current_player(request.user)
    if request.method == 'GET':
        return Response(profile_settings_payload(player))

    serializer = PlayerProfileUpdateCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    with transaction.atomic():
        locked_player = Player.objects.select_for_update().get(pk=player.pk)
        pending = (
            PlayerProfileUpdateRequest.objects
            .select_for_update()
            .filter(player=locked_player, status=PlayerProfileUpdateRequest.STATUS_PENDING)
            .first()
        )
        if pending:
            pending.bio = data['bio']
            pending.audio_intro_url = data['audio_intro_url']
            pending.audio_intro_title = data['audio_intro_title']
            pending.reject_reason = ''
            pending.save(update_fields=['bio', 'audio_intro_url', 'audio_intro_title', 'reject_reason'])
            update_request = pending
        else:
            update_request = PlayerProfileUpdateRequest.objects.create(
                player=locked_player,
                bio=data['bio'],
                audio_intro_url=data['audio_intro_url'],
                audio_intro_title=data['audio_intro_title'],
            )
    return Response({
        'message': '资料修改已提交审核，审核期间继续展示原资料',
        'update_request': PlayerProfileUpdateRequestSerializer(update_request).data,
        **profile_settings_payload(player),
    })
