from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import ClientProfile
from apps.catalog.models import Addon, Package, PackageGroup, PlayerType
from apps.catalog.serializers import AddonSerializer, PackageGroupSerializer, PackageSerializer, PlayerTypeSerializer
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


@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def packages(request):
    if request.method == 'GET':
        return Response(PackageSerializer(Package.objects.select_related('group').all(), many=True).data)
    group = PackageGroup.objects.filter(id=request.data.get('group_id')).first() if request.data.get('group_id') else None
    package = Package.objects.create(
        name=request.data.get('name', ''),
        player_count=request.data.get('player_count') or 1,
        base_price=request.data.get('base_price') or 0,
        description=request.data.get('description') or '',
        is_custom=bool(request.data.get('is_custom', False)),
        group=group,
    )
    return Response(PackageSerializer(package).data, status=status.HTTP_201_CREATED)


@api_view(['PUT'])
@permission_classes([IsAdminUser])
def package_detail(request, package_id):
    package = Package.objects.filter(id=package_id).first()
    if not package:
        return Response({'detail': '套餐不存在'}, status=status.HTTP_404_NOT_FOUND)
    for field in ['name', 'player_count', 'base_price', 'description', 'is_custom', 'is_active']:
        if field in request.data:
            setattr(package, field, request.data[field])
    if 'group_id' in request.data:
        package.group = PackageGroup.objects.filter(id=request.data.get('group_id')).first()
    package.save()
    return Response(PackageSerializer(package).data)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def disable_package(request, package_id):
    Package.objects.filter(id=package_id).update(is_active=False)
    return Response({'message': '套餐已下架'})


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
        return Response(PackageGroupSerializer(PackageGroup.objects.all(), many=True).data)
    group = PackageGroup.objects.create(
        name=request.data.get('name', ''),
        sort_order=request.data.get('sort_order') or 0,
    )
    return Response(PackageGroupSerializer(group).data, status=status.HTTP_201_CREATED)
