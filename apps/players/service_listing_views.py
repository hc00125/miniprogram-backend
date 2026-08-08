from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.catalog.models import Package, PackageSpec
from apps.common.permissions import IsApprovedPlayer, current_player

from .escort_qualification import player_has_approved_escort_qualification
from .models import PlayerServiceListing


def serialize_spec(spec):
    package = spec.package
    required_type = spec.required_player_type
    return {
        'id': spec.id,
        'package_id': package.id,
        'package_name': package.name,
        'package_description': package.description or '',
        'package_cover_url': package.cover_url or package.image_url or package.thumb_url or package.picture_url or '',
        'name': spec.name,
        'display_name': spec.display_name or spec.short_name or spec.name,
        'description': spec.description or '',
        'price': spec.price,
        'required_player_type_id': spec.required_player_type_id,
        'required_player_type_name': required_type.name if required_type else '',
        'requires_escort_qualification': package.requires_escort_qualification,
        'sort_order': spec.sort_order,
    }


def serialize_listing(listing):
    return {
        'id': listing.id,
        'status': listing.status,
        'status_text': listing.get_status_display(),
        'is_available': listing.is_available,
        'custom_description': listing.custom_description,
        'sort_order': listing.sort_order,
        'rejection_reason': listing.rejection_reason,
        'reviewed_at': listing.reviewed_at.isoformat() if listing.reviewed_at else None,
        'created_at': listing.created_at.isoformat(),
        'updated_at': listing.updated_at.isoformat(),
        'spec': serialize_spec(listing.spec),
    }


def shared_specs_queryset():
    """Return platform-owned, one-player specs that can already be paid for.

    The shared layer never exposes legacy player-owned products, multi-player
    packages, guarantee products or a price that has no active virtual-payment
    binding. This keeps self-listing from creating an order that cannot pay.
    """
    payment_binding = (
        Q(virtual_payment_bindings__is_active=True)
        | Q(
            package__virtual_payment_bindings__is_active=True,
            package__virtual_payment_bindings__spec__isnull=True,
        )
    )
    return (
        PackageSpec.objects
        .filter(
            payment_binding,
            is_active=True,
            package__is_active=True,
            package__selling_mode=Package.SELLING_MODE_PUBLIC,
            package__owner_player__isnull=True,
            package__player_count=1,
            package__product_type__in=[
                Package.PRODUCT_TYPE_NORMAL,
                Package.PRODUCT_TYPE_FUN,
                Package.PRODUCT_TYPE_SPECIAL,
            ],
        )
        .select_related('package', 'required_player_type')
        .distinct()
        .order_by('package__sort_order', 'package_id', 'sort_order', 'id')
    )


ESCORT_REVIEW_ERROR = '护航类服务需要管理员人工审核资格'


def auto_review(listing):
    """Decide the target status for a newly submitted/resubmitted listing.

    Returns (status, message, rejection_reason).
    - approved: all auto-approval checks pass
    - rejected: a hard requirement fails (1-4), or escort product without escort qualification
    - pending:  escort product whose player holds approved escort qualification (manual review)
    """
    if not getattr(settings, 'PLAYER_SERVICE_LISTING_AUTO_APPROVE', False):
        return PlayerServiceListing.STATUS_PENDING, '上架申请已提交，等待管理员审核', ''
    error = listing.automatic_approval_error()
    if not error:
        return PlayerServiceListing.STATUS_APPROVED, '服务已自动上架', ''
    if error == ESCORT_REVIEW_ERROR:
        if player_has_approved_escort_qualification(listing.player):
            return PlayerServiceListing.STATUS_PENDING, '护航类商品需管理员人工审核', ''
        reason = '无护航资格，无法上架护航类商品'
        return PlayerServiceListing.STATUS_REJECTED, reason, reason
    return PlayerServiceListing.STATUS_REJECTED, error, error


@api_view(['GET', 'POST'])
@permission_classes([IsApprovedPlayer])
def service_listings(request):
    player = current_player(request.user)

    if request.method == 'GET':
        listings = list(
            PlayerServiceListing.objects
            .filter(player=player)
            .select_related('spec__package', 'spec__required_player_type')
            .order_by('sort_order', 'id')
        )
        listed_spec_ids = {item.spec_id for item in listings}
        available_specs = [
            serialize_spec(spec)
            for spec in shared_specs_queryset()
            if spec.id not in listed_spec_ids
        ]
        return Response({
            'auto_approval_enabled': bool(getattr(settings, 'PLAYER_SERVICE_LISTING_AUTO_APPROVE', False)),
            'listings': [serialize_listing(item) for item in listings],
            'available_specs': available_specs,
        })

    spec_id = request.data.get('spec_id')
    spec = shared_specs_queryset().filter(pk=spec_id).first()
    if not spec:
        return Response({'detail': '共享服务规格不存在、未绑定虚拟道具或已下架'}, status=status.HTTP_400_BAD_REQUEST)

    custom_description = str(request.data.get('custom_description') or '').strip()[:300]
    with transaction.atomic():
        listing, created = PlayerServiceListing.objects.select_for_update().get_or_create(
            player=player,
            spec=spec,
            defaults={
                'custom_description': custom_description,
                'status': PlayerServiceListing.STATUS_PENDING,
            },
        )
        if not created:
            if listing.status in {PlayerServiceListing.STATUS_APPROVED, PlayerServiceListing.STATUS_PENDING}:
                return Response({'detail': '该服务已经上架或正在审核'}, status=status.HTTP_400_BAD_REQUEST)
            listing.status = PlayerServiceListing.STATUS_PENDING
            listing.is_available = True
            listing.custom_description = custom_description
            listing.rejection_reason = ''
            listing.reviewed_by = None
            listing.reviewed_at = None

        target_status, message, reject_reason = auto_review(listing)
        listing.status = target_status
        if target_status == PlayerServiceListing.STATUS_APPROVED:
            listing.reviewed_at = timezone.now()
        elif target_status == PlayerServiceListing.STATUS_REJECTED:
            listing.is_available = False
            listing.reviewed_at = timezone.now()
            listing.rejection_reason = reject_reason
        listing.save()

    return Response(
        {'message': message, 'listing': serialize_listing(listing)},
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(['PATCH'])
@permission_classes([IsApprovedPlayer])
def service_listing_detail(request, listing_id):
    player = current_player(request.user)
    listing = (
        PlayerServiceListing.objects
        .filter(pk=listing_id, player=player)
        .select_related('spec__package', 'spec__required_player_type')
        .first()
    )
    if not listing:
        return Response({'detail': '服务上架记录不存在'}, status=status.HTTP_404_NOT_FOUND)

    action = str(request.data.get('action') or '').strip().lower()
    if action == 'offline':
        listing.status = PlayerServiceListing.STATUS_OFFLINE
        listing.is_available = False
    elif action in {'restore', 'resubmit'}:
        if not shared_specs_queryset().filter(pk=listing.spec_id).exists():
            return Response({'detail': '该共享规格已下架或支付绑定已失效'}, status=status.HTTP_400_BAD_REQUEST)
        listing.status = PlayerServiceListing.STATUS_PENDING
        listing.is_available = True
        listing.rejection_reason = ''
        listing.reviewed_by = None
        listing.reviewed_at = None
        target_status, _, reject_reason = auto_review(listing)
        listing.status = target_status
        if target_status == PlayerServiceListing.STATUS_APPROVED:
            listing.reviewed_at = timezone.now()
        elif target_status == PlayerServiceListing.STATUS_REJECTED:
            listing.is_available = False
            listing.reviewed_at = timezone.now()
            listing.rejection_reason = reject_reason
    elif action:
        return Response({'detail': '不支持的服务操作'}, status=status.HTTP_400_BAD_REQUEST)

    if 'is_available' in request.data and listing.status == PlayerServiceListing.STATUS_APPROVED:
        listing.is_available = request.data.get('is_available') is True
    if 'custom_description' in request.data:
        listing.custom_description = str(request.data.get('custom_description') or '').strip()[:300]
    if 'sort_order' in request.data:
        try:
            listing.sort_order = int(request.data.get('sort_order') or 0)
        except (TypeError, ValueError):
            return Response({'detail': '排序值必须是整数'}, status=status.HTTP_400_BAD_REQUEST)

    listing.save()
    return Response({
        'message': '服务设置已更新',
        'listing': serialize_listing(listing),
    })
