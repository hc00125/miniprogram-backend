from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.players.models import Player
from .authentication import ReadOnlyLegacyAuthentication
from .models import CrownGrant, PatronagePurchase
from .pricing import catalog_data, player_is_available
from django.http import Http404
from .quotes import quote_data
from .serializers import CrownSerializer, PlayerInputSerializer, QuoteInputSerializer, RecordSerializer
from .serializers import PurchaseInputSerializer, KeyInputSerializer


def query_player(request):
    serializer = PlayerInputSerializer(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    player = get_object_or_404(
        Player.objects.select_related('user__client_profile'),
        pk=serializer.validated_data['player_id'],
    )
    if not player.is_archived and not player_is_available(player):
        raise Http404
    return player


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def catalog(request):
    player = query_player(request) if 'player_id' in request.query_params else None
    return Response(catalog_data(player))


@api_view(['POST'])
@authentication_classes([ReadOnlyLegacyAuthentication, JWTAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def quotes(request):
    serializer = QuoteInputSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    player = get_object_or_404(Player.objects.select_related('user__client_profile'), pk=serializer.validated_data['player_id'])
    return Response(quote_data(request.user, player, serializer.validated_data['package_code']))


@api_view(['POST'])
@authentication_classes([ReadOnlyLegacyAuthentication, JWTAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def purchases(request):
    from .purchases import purchase
    from rest_framework.exceptions import APIException
    serializer = PurchaseInputSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        record = purchase(request.user, **serializer.validated_data)
    except (APIException, Http404):
        raise
    except Exception as exc:
        # No login codes, upstream payloads or exception text in responses/logs.
        import logging
        logging.getLogger(__name__).error('Patronage purchase requires review: %s', type(exc).__name__)
        return Response({'code': 'PURCHASE_REQUIRES_RECOVERY',
            'detail': '请保留原购买编号并查询原单，勿重复支付；必要时联系客服。'}, status=503)
    return Response(RecordSerializer(record).data)


@api_view(['GET'])
@authentication_classes([ReadOnlyLegacyAuthentication, JWTAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def purchase_by_key(request):
    serializer = KeyInputSerializer(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    record = get_object_or_404(PatronagePurchase.objects.select_related('attempt'),
        boss=request.user, **serializer.validated_data)
    return Response(RecordSerializer(record).data)


@api_view(['GET'])
@authentication_classes([ReadOnlyLegacyAuthentication, JWTAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def pending_purchases(request):
    from django.db.models import Q
    from apps.wallet.models import WalletSpendAttempt
    queryset = PatronagePurchase.objects.filter(boss=request.user).filter(
        Q(payment_status__in=('created', 'processing', 'unknown'))
        | Q(attempt__status__in=WalletSpendAttempt.ACTIVE)).select_related('attempt', 'earning')
    paginator = PageNumberPagination()
    paginator.page_size = 20
    paginator.page_size_query_param = 'page_size'
    paginator.max_page_size = 100
    page = paginator.paginate_queryset(queryset, request)
    return paginator.get_paginated_response(RecordSerializer(page, many=True).data)


@api_view(['GET'])
@authentication_classes([ReadOnlyLegacyAuthentication, JWTAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def records(request):
    paginator = PageNumberPagination()
    paginator.page_size = 20
    paginator.page_size_query_param = 'page_size'
    paginator.max_page_size = 100
    queryset = PatronagePurchase.objects.filter(boss=request.user).select_related('attempt', 'earning')
    page = paginator.paginate_queryset(queryset, request)
    return paginator.get_paginated_response(RecordSerializer(page, many=True).data)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def crowns(request):
    player = query_player(request)
    now = timezone.now()
    grants = CrownGrant.objects.filter(
        purchase__player=player, purchase__payment_status='paid',
        starts_at__lte=now, expires_at__gt=now, revoked_at__isnull=True,
    ).select_related('purchase__boss__client_profile').order_by('-expires_at', '-id')
    # Renewal receipts retain their own audit identities. Display only the
    # longest active right per boss; different bosses continue to coexist.
    active_by_boss = {}
    for grant in grants:
        active_by_boss.setdefault(grant.purchase.boss_id, grant)
    return Response({'results': CrownSerializer(list(active_by_boss.values()), many=True).data})
