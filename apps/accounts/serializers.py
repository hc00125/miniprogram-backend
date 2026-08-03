from decimal import Decimal

from rest_framework import serializers

from apps.players.models import PlayerApplication
from apps.players.serializers import PlayerApplicationSerializer, PlayerSerializer
from apps.wallet.diamonds import DIAMONDS_PER_YUAN, yuan_to_diamonds

from .access import refresh_expired_account_restriction
from .models import ClientProfile
from .vip import qmoney, vip_snapshot


class ClientProfileSerializer(serializers.ModelSerializer):
    application = serializers.SerializerMethodField()
    player = serializers.SerializerMethodField()
    vip = serializers.SerializerMethodField()
    wallet = serializers.SerializerMethodField()
    cumulative_consumption_diamonds = serializers.SerializerMethodField()
    account_status_text = serializers.CharField(source='get_account_status_display', read_only=True)

    class Meta:
        model = ClientProfile
        fields = [
            'id', 'openid', 'nickname', 'nickname_customized', 'avatar_url', 'role', 'player_status',
            'account_status', 'account_status_text', 'account_suspended_until', 'account_restriction_reason',
            'cumulative_consumption', 'cumulative_consumption_diamonds', 'vip', 'wallet',
            'created_at', 'application', 'player',
        ]

    def to_representation(self, instance):
        refresh_expired_account_restriction(instance)
        return super().to_representation(instance)

    def get_cumulative_consumption_diamonds(self, obj):
        return yuan_to_diamonds(obj.cumulative_consumption)

    def get_wallet(self, obj):
        from apps.wallet.models import ClientWallet

        wallet = ClientWallet.objects.filter(profile=obj).only('balance').first()
        balance = wallet.balance if wallet else Decimal('0.00')
        return {
            'balance': str(qmoney(balance)),
            'balance_yuan': str(qmoney(balance)),
            'balance_diamonds': yuan_to_diamonds(balance),
            'diamonds_per_yuan': DIAMONDS_PER_YUAN,
        }

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
        snapshot = vip_snapshot(obj)
        snapshot.update({
            'growth_diamonds': yuan_to_diamonds(snapshot.get('cumulative_consumption')),
            'remaining_growth_diamonds': yuan_to_diamonds(snapshot.get('remaining_to_next')),
            'diamonds_per_yuan': DIAMONDS_PER_YUAN,
        })
        for key in ('current_tier', 'next_tier'):
            tier = snapshot.get(key)
            if tier:
                tier['min_growth_diamonds'] = yuan_to_diamonds(tier.get('min_consumption'))
        return snapshot


class WechatLoginSerializer(serializers.Serializer):
    code = serializers.CharField(required=False, allow_blank=True)
    nickname = serializers.CharField(required=False, allow_blank=True, default='')
    avatar_url = serializers.URLField(required=False, allow_blank=True, default='')
    openid = serializers.CharField(required=False, allow_blank=True, default='')

    def validate(self, attrs):
        if not attrs.get('code') and not attrs.get('openid'):
            raise serializers.ValidationError({'detail': '缺少微信登录 code'})
        return attrs
