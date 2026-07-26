from decimal import Decimal

from rest_framework import serializers

from apps.players.models import PlayerApplication
from apps.players.serializers import PlayerApplicationSerializer, PlayerSerializer

from .models import ClientProfile
from .vip import qmoney, vip_snapshot


class ClientProfileSerializer(serializers.ModelSerializer):
    application = serializers.SerializerMethodField()
    player = serializers.SerializerMethodField()
    vip = serializers.SerializerMethodField()
    wallet = serializers.SerializerMethodField()

    class Meta:
        model = ClientProfile
        fields = [
            'id', 'openid', 'nickname', 'nickname_customized', 'avatar_url', 'role', 'player_status',
            'cumulative_consumption', 'vip', 'wallet', 'created_at', 'application', 'player',
        ]

    def get_wallet(self, obj):
        from apps.wallet.models import ClientWallet

        wallet = ClientWallet.objects.filter(profile=obj).only('balance').first()
        balance = wallet.balance if wallet else Decimal('0.00')
        return {'balance': str(qmoney(balance))}

    def get_application(self, obj):
        application = PlayerApplication.objects.filter(user=obj.user).order_by('-submitted_at').first()
        if not application:
            return None
        return PlayerApplicationSerializer(application).data

    def get_player(self, obj):
        player = getattr(obj.user, 'player_profile', None)
        if not player:
            return None
        return PlayerSerializer(player).data

    def get_vip(self, obj):
        return vip_snapshot(obj)


class WechatLoginSerializer(serializers.Serializer):
    code = serializers.CharField(required=False, allow_blank=True)
    nickname = serializers.CharField(required=False, allow_blank=True, default='')
    avatar_url = serializers.URLField(required=False, allow_blank=True, default='')
    openid = serializers.CharField(required=False, allow_blank=True, default='')

    def validate(self, attrs):
        if not attrs.get('code') and not attrs.get('openid'):
            raise serializers.ValidationError({'detail': '缺少微信登录 code'})
        return attrs
