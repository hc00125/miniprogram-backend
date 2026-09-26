from django.db.models import Prefetch, Q
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.accounts.access import account_restriction_payload
from apps.players.models import Player, PlayerServiceListing

from .models import Package, PackageImage, PackageSpec, PlayerOffer
from .serializers import PackageSerializer, PlayerOfferSerializer


def player_service_products_queryset(player_id):
    """Legacy personal products kept for existing data and historical orders."""
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


def shared_listing_products(player, request):
    payment_binding = (
        Q(spec__virtual_payment_bindings__is_active=True)
        | Q(
            spec__package__virtual_payment_bindings__is_active=True,
            spec__package__virtual_payment_bindings__spec__isnull=True,
        )
    )
    listings = list(
        PlayerServiceListing.objects
        .filter(
            payment_binding,
            player=player,
            status=PlayerServiceListing.STATUS_APPROVED,
            is_available=True,
            spec__is_active=True,
            spec__package__is_active=True,
        )
        .select_related(
            'spec__required_player_type',
            'spec__package__group__game_service',
        )
        .distinct()
        .order_by('sort_order', 'spec__package__sort_order', 'spec__sort_order', 'id')
    )
    if not listings:
        return []

    packages = []
    package_map = {}
    listing_map = {}
    for listing in listings:
        package = listing.spec.package
        if package.id not in package_map:
            package.active_specs = []
            package_map[package.id] = package
            packages.append(package)
        package_map[package.id].active_specs.append(listing.spec)
        listing_map[listing.spec_id] = listing

    image_queryset = PackageImage.objects.filter(
        package_id__in=package_map,
        is_active=True,
    ).order_by('image_type', 'sort_order', 'id')
    images_by_package = {}
    for image in image_queryset:
        images_by_package.setdefault(image.package_id, []).append(image)
    for package in packages:
        package.active_images = images_by_package.get(package.id, [])

    payload = PackageSerializer(packages, many=True, context={'request': request}).data
    for product in payload:
        product['selling_mode'] = Package.SELLING_MODE_PLAYER_DESIGNATED
        product['owner_player_id'] = player.id
        product['owner_player_name'] = player.name
        product['owner_player_type_name'] = player.player_type.name if player.player_type_id else None
        product_listing_ids = []
        for spec_payload in product.get('specs') or []:
            listing = listing_map.get(spec_payload.get('id'))
            if not listing:
                continue
            spec_payload['listing_id'] = listing.id
            spec_payload['listing_status'] = listing.status
            spec_payload['listing_is_available'] = listing.is_available
            spec_payload['listing_description'] = listing.custom_description
            product_listing_ids.append(listing.id)
        product['listing_id'] = product_listing_ids[0] if len(product_listing_ids) == 1 else None
    return payload


@api_view(['GET'])
@permission_classes([AllowAny])
def player_service_products(request, player_id):
    """Return the player's currently sellable services.

    Shared service listings are the source of truth once a player has entered
    the listing system. Legacy personal products are returned only for players
    that have never created a PlayerServiceListing record, preserving old
    accounts without allowing offline shared listings to reappear through a
    stale player-owned Package.
    """
    player = Player.objects.filter(
        pk=player_id,
        status=Player.STATUS_APPROVED,
        can_be_designated=True,
        is_publicly_visible=True,
    ).select_related('player_type', 'user__client_profile').first()
    if not player or (player.user and account_restriction_payload(player.user)):
        return Response({'detail': '陪玩师不存在或暂不接受指定'}, status=status.HTTP_404_NOT_FOUND)

    has_listing_records = PlayerServiceListing.objects.filter(player=player).exists()
    products = shared_listing_products(player, request)

    if not has_listing_records:
        legacy_products = PackageSerializer(
            player_service_products_queryset(player.id),
            many=True,
            context={'request': request},
        ).data
        products.extend(legacy_products)

    products.sort(key=lambda item: (int(item.get('sort_order') or 0), int(item.get('id') or 0)))
    return Response({
        'player_id': player.id,
        'player_name': player.name,
        'products': products,
    })


@api_view(['GET'])
@permission_classes([AllowAny])
def player_offers(request, player_id):
    """Legacy package-family offer endpoint retained until its callers are removed."""
    player = Player.objects.filter(
        pk=player_id,
        status=Player.STATUS_APPROVED,
    ).select_related('user__client_profile').first()
    if not player or (player.user and account_restriction_payload(player.user)):
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
