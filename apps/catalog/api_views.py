from django.db.models import Prefetch
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.players.models import Player

from .models import Package, PackageImage, PackageSpec, PlayerOffer
from .serializers import PackageSerializer, PlayerOfferSerializer


def player_service_products_queryset(player_id):
    return (
        Package.objects
        .filter(
            owner_player_id=player_id,
            selling_mode=Package.SELLING_MODE_PLAYER_DESIGNATED,
            is_active=True,
        )
        .select_related('owner_player__player_type', 'group', 'group__game_service')
        .prefetch_related(
            Prefetch(
                'specs',
                queryset=PackageSpec.objects.filter(is_active=True).select_related('required_player_type').order_by('sort_order', 'id'),
                to_attr='active_specs',
            ),
            Prefetch(
                'images',
                queryset=PackageImage.objects.filter(is_active=True).order_by('image_type', 'sort_order', 'id'),
                to_attr='active_images',
            ),
        )
        .order_by('sort_order', 'id')
    )


@api_view(['GET'])
@permission_classes([AllowAny])
def player_service_products(request, player_id):
    """Return the active, one-person products owned by this player.

    The endpoint deliberately exposes only products whose owner is the requested
    player, so clients never choose or submit an arbitrary target player id.
    """
    player = Player.objects.filter(
        pk=player_id,
        status=Player.STATUS_APPROVED,
        can_be_designated=True,
        is_publicly_visible=True,
    ).first()
    if not player:
        return Response({'detail': '陪玩师不存在或暂不接受指定'}, status=status.HTTP_404_NOT_FOUND)

    products = player_service_products_queryset(player.id)
    return Response({
        'player_id': player.id,
        'player_name': player.name,
        'products': PackageSerializer(products, many=True, context={'request': request}).data,
    })


@api_view(['GET'])
@permission_classes([AllowAny])
def player_offers(request, player_id):
    """List equipment families that may be selected for one designated player.

    This is deliberately a catalog-only endpoint: it exposes configured offers
    and static package/spec compatibility, while an order draft still owns
    availability conflicts, the selected duration, and the final quote.
    """
    player = Player.objects.filter(
        pk=player_id,
        status=Player.STATUS_APPROVED,
    ).first()
    if not player:
        return Response({'detail': '陪玩师不存在或未通过审核'}, status=status.HTTP_404_NOT_FOUND)

    offers = PlayerOffer.objects.available().filter(player_id=player.id)
    package_family_id = request.query_params.get('package_family_id')
    if package_family_id:
        offers = offers.filter(package_family_id=package_family_id)

    offers = offers.select_related(
        'player__player_type',
        'player__minimum_designated_player_type',
        'package_family__game_service',
    ).prefetch_related(
        Prefetch(
            'package_family__packages',
            queryset=Package.objects.filter(is_active=True).select_related(
                'group', 'group__game_service', 'package_family',
            ).prefetch_related('specs__required_player_type').order_by('player_count', 'sort_order', 'id'),
            to_attr='active_packages',
        ),
    ).order_by('sort_order', 'id')

    return Response({
        'player_id': player.id,
        'offers': PlayerOfferSerializer(offers, many=True, context={'request': request}).data,
    })
