from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import ClientProfile
from apps.catalog.models import Addon, GameService, Package, PackageGroup, PackageSpec, PlayerType
from apps.catalog.serializers import (
    AddonSerializer,
    PackageGroupSerializer,
    PackageSerializer,
    PackageSpecSerializer,
    PackageWriteSerializer,
    PlayerTypeSerializer,
)
from apps.common.permissions import IsAdminUser
from apps.orders.models import Order
from apps.orders.serializers import BossOrderDetailSerializer, BossOrderListSerializer
from apps.players.models import Player, PlayerApplication
from apps.players.serializers import PlayerApplicationApproveSerializer, PlayerApplicationRejectSerializer, PlayerApplicationSerializer


def admin_payload(user):
    return {
        'id': user.id,
        'username': user.username,
        'is_super': user.is_superuser,
        'must_change_password': False,
    }


@api_view(['POST'])
@permission_classes([AllowAny])
def login(request):
    username = request.data.get('username')
    password = request.data.get('password')
    user = User.objects.filter(username=username).first()
    if not user or not user.check_password(password):
        return Response({'detail': '用户名或密码错误'}, status=status.HTTP_400_BAD_REQUEST)
    if not user.is_staff:
        return Response({'detail': '账号未通过审批'}, status=status.HTTP_403_FORBIDDEN)
    token = str(RefreshToken.for_user(user).access_token)
    user.last_login = timezone.now()
    user.save(update_fields=['last_login'])
    return Response({'token': token, 'admin': admin_payload(user)})


@api_view(['GET'])
@permission_classes([IsAdminUser])
def me(request):
    return Response({**admin_payload(request.user), 'status': '已通过'})


@api_view(['GET'])
@permission_classes([IsAdminUser])
def player_applications(request):
    status_filter = request.query_params.get('status')
    qs = PlayerApplication.objects.select_related('user', 'user__client_profile').all()
    if status_filter:
        qs = qs.filter(status=status_filter)
    return Response(PlayerApplicationSerializer(qs, many=True).data)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def approve_application(request, application_id):
    application = PlayerApplication.objects.filter(id=application_id).select_related('user', 'player_type').first()
    if not application:
        return Response({'detail': '申请不存在'}, status=status.HTTP_404_NOT_FOUND)
    serializer = PlayerApplicationApproveSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    player_type_id = serializer.validated_data.get('player_type_id')
    player_type = PlayerType.objects.filter(id=player_type_id).first() if player_type_id else application.player_type
    if not player_type:
        return Response({'detail': '请选择打手类型'}, status=status.HTTP_400_BAD_REQUEST)

    player, _ = Player.objects.update_or_create(
        user=application.user,
        defaults={
            'name': application.name,
            'player_type': player_type,
            'contact_wechat': application.contact_wechat,
            'bio': application.bio,
            'status': Player.STATUS_APPROVED,
        },
    )
    application.status = PlayerApplication.STATUS_APPROVED
    application.remark = serializer.validated_data.get('remark', '')
    application.reviewed_by = request.user
    application.reviewed_at = timezone.now()
    application.save()
    profile = getattr(application.user, 'client_profile', None)
    if profile:
        profile.player_status = ClientProfile.PLAYER_STATUS_APPROVED
        profile.save(update_fields=['player_status', 'updated_at'])
    return Response({'message': '审核通过', 'application': PlayerApplicationSerializer(application).data, 'player_id': player.id})


@api_view(['POST'])
@permission_classes([IsAdminUser])
def reject_application(request, application_id):
    application = PlayerApplication.objects.filter(id=application_id).select_related('user').first()
    if not application:
        return Response({'detail': '申请不存在'}, status=status.HTTP_404_NOT_FOUND)
    serializer = PlayerApplicationRejectSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    application.status = PlayerApplication.STATUS_REJECTED
    application.reject_reason = serializer.validated_data['reject_reason']
    application.reviewed_by = request.user
    application.reviewed_at = timezone.now()
    application.save()
    profile = getattr(application.user, 'client_profile', None)
    if profile:
        profile.player_status = ClientProfile.PLAYER_STATUS_REJECTED
        profile.save(update_fields=['player_status', 'updated_at'])
    return Response({'message': '已拒绝', 'application': PlayerApplicationSerializer(application).data})


@api_view(['GET'])
@permission_classes([IsAdminUser])
def orders(request):
    status_filter = request.query_params.get('status')
    qs = Order.objects.select_related('package', 'addon').all()
    if status_filter:
        qs = qs.filter(status=status_filter)
    return Response(BossOrderListSerializer(qs[:100], many=True).data)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def order_detail(request, order_no):
    order = Order.objects.filter(order_no=order_no).select_related('package', 'addon').prefetch_related('order_players__player__player_type').first()
    if not order:
        return Response({'detail': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)
    return Response(BossOrderDetailSerializer(order).data)


# ──────────────────────────────────────────
#  商品管理
# ──────────────────────────────────────────

@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def packages(request):
    if request.method == 'GET':
        qs = Package.objects.select_related('group', 'group__game_service').prefetch_related('specs').all()
        return Response(PackageSerializer(qs, many=True).data)

    serializer = PackageWriteSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    package = serializer.save()
    return Response(PackageSerializer(package).data, status=status.HTTP_201_CREATED)


@api_view(['PUT'])
@permission_classes([IsAdminUser])
def package_detail(request, package_id):
    package = Package.objects.filter(id=package_id).first()
    if not package:
        return Response({'detail': '商品不存在'}, status=status.HTTP_404_NOT_FOUND)
    serializer = PackageWriteSerializer(package, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    updated = serializer.save()
    return Response(PackageSerializer(updated).data)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def disable_package(request, package_id):
    Package.objects.filter(id=package_id).update(is_active=False)
    return Response({'message': '商品已下架'})


# ──────────────────────────────────────────
#  规格管理
# ──────────────────────────────────────────

@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def specs(request, package_id):
    if request.method == 'GET':
        qs = PackageSpec.objects.filter(package_id=package_id).order_by('sort_order', 'id')
        return Response(PackageSpecSerializer(qs, many=True).data)

    if not Package.objects.filter(id=package_id).exists():
        return Response({'detail': '商品不存在'}, status=status.HTTP_404_NOT_FOUND)
    serializer = PackageSpecSerializer(data={**request.data, 'package_id': package_id})
    serializer.is_valid(raise_exception=True)
    spec = serializer.save()
    return Response(PackageSpecSerializer(spec).data, status=status.HTTP_201_CREATED)


@api_view(['PUT'])
@permission_classes([IsAdminUser])
def spec_detail(request, package_id, spec_id):
    spec = PackageSpec.objects.filter(id=spec_id, package_id=package_id).first()
    if not spec:
        return Response({'detail': '规格不存在'}, status=status.HTTP_404_NOT_FOUND)
    serializer = PackageSpecSerializer(spec, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    updated = serializer.save()
    return Response(PackageSpecSerializer(updated).data)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def disable_spec(request, package_id, spec_id):
    PackageSpec.objects.filter(id=spec_id, package_id=package_id).update(is_active=False)
    return Response({'message': '规格已禁用'})


# ──────────────────────────────────────────
#  分类 / 加价 / 陪玩类型
# ──────────────────────────────────────────

@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def addons(request):
    if request.method == 'GET':
        return Response(AddonSerializer(Addon.objects.all(), many=True).data)
    addon = Addon.objects.create(
        name=request.data.get('name', ''),
        price_per_player=request.data.get('price_per_player') or 0,
        priority=request.data.get('priority') or 0,
    )
    return Response(AddonSerializer(addon).data, status=status.HTTP_201_CREATED)


@api_view(['PUT'])
@permission_classes([IsAdminUser])
def addon_detail(request, addon_id):
    addon = Addon.objects.filter(id=addon_id).first()
    if not addon:
        return Response({'detail': '特殊陪不存在'}, status=status.HTTP_404_NOT_FOUND)
    for field in ['name', 'price_per_player', 'priority', 'is_active']:
        if field in request.data:
            setattr(addon, field, request.data[field])
    addon.save()
    return Response(AddonSerializer(addon).data)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def disable_addon(request, addon_id):
    Addon.objects.filter(id=addon_id).update(is_active=False)
    return Response({'message': '特殊陪已下架'})


@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def package_groups(request):
    if request.method == 'GET':
        qs = PackageGroup.objects.select_related('game_service').all()
        return Response(PackageGroupSerializer(qs, many=True).data)

    game_service_id = request.data.get('game_service_id')
    game_service = GameService.objects.filter(id=game_service_id).first() if game_service_id else None
    if not game_service:
        game_service = GameService.objects.filter(code='arena-breakout').first()
    if not game_service:
        return Response({'detail': '请先在Django后台创建游戏服务'}, status=status.HTTP_400_BAD_REQUEST)

    group = PackageGroup.objects.create(
        game_service=game_service,
        name=request.data.get('name', ''),
        sort_order=request.data.get('sort_order') or 0,
    )
    return Response(PackageGroupSerializer(group).data, status=status.HTTP_201_CREATED)
