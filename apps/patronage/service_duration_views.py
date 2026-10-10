from decimal import Decimal
from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import serializers
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication
from apps.accounts.models import ClientProfile
from apps.common.permissions import current_player, IsApprovedPlayer
from apps.players.models import Player
from .authentication import ReadOnlyLegacyAuthentication
from .models import PlayerNamingServiceConfig
from .pricing import catalog_data
from .service_duration import DEFAULT_HOURS


class DurationInput(serializers.Serializer):
    package_code = serializers.ChoiceField(choices=tuple(DEFAULT_HOURS))
    # Two input decimal places allow us to reject quarter-hours, never round them.
    service_hours = serializers.DecimalField(max_digits=13, decimal_places=2, min_value=Decimal('.5'), max_value=Decimal('99999999999.5'))

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) != {'package_code', 'service_hours'}:
            raise serializers.ValidationError({'detail': '仅接受冠名档位与服务小时数'})
        if isinstance(data.get('service_hours'), bool):
            raise serializers.ValidationError({'service_hours': '服务小时须为0.5小时步长'})
        return super().to_internal_value(data)

    def validate_service_hours(self, value):
        if value % Decimal('.5'):
            raise serializers.ValidationError('服务小时须为0.5小时步长，不会自动舍入')
        return value


def ensure_owner(user, player):
    profile = ClientProfile.objects.filter(user=user).first()
    from apps.common.player_model_version import is_archived
    if not player or player.user_id != user.pk or is_archived(player) or player.status != Player.STATUS_APPROVED:
        raise PermissionDenied('仅本人已审核且未归档的陪玩可设置')
    if not user.is_active or (profile and profile.account_status != 'active'):
        raise PermissionDenied('账号当前不可修改服务')


def result(player):
    data = catalog_data(player)
    data['packages'] = [p for p in data['packages'] if p['kind'] == 'naming']
    return data


@api_view(['GET', 'POST'])
@authentication_classes([ReadOnlyLegacyAuthentication, JWTAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, IsApprovedPlayer])
def my_service_durations(request):
    player = current_player(request.user)
    ensure_owner(request.user, player)
    if request.method == 'GET':
        return Response(result(player))
    value = DurationInput(data=request.data)
    value.is_valid(raise_exception=True)
    with transaction.atomic():
        # Same user/profile -> Player ordering as purchase configuration locks.
        # Player is also the stable lock for an as-yet absent config row.
        user = get_user_model().objects.select_for_update().get(pk=request.user.pk)
        list(ClientProfile.objects.select_for_update().filter(user=user))
        player = Player.objects.select_for_update().get(pk=player.pk)
        ensure_owner(user, player)
        row = PlayerNamingServiceConfig.objects.select_for_update().filter(player=player, package_code=value.validated_data['package_code']).first()
        if row is None:
            PlayerNamingServiceConfig.objects.create(player=player, **value.validated_data)
        elif row.service_hours != value.validated_data['service_hours']:
            row.service_hours = value.validated_data['service_hours']
            row.save(update_fields=['service_hours', 'updated_at'])
        return Response(result(player))
