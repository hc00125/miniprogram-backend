from rest_framework import serializers

from .models import ClientProfile
from apps.players.models import PlayerApplication
from apps.players.serializers import PlayerSerializer, PlayerApplicationSerializer


class ClientProfileSerializer(serializers.ModelSerializer):
    application = serializers.SerializerMethodField()
    player = serializers.SerializerMethodField()

    class Meta:
        model = ClientProfile
        fields = ['id', 'openid', 'nickname', 'avatar_url', 'role', 'player_status', 'created_at', 'application', 'player']

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


class WechatLoginSerializer(serializers.Serializer):
    code = serializers.CharField(required=False, allow_blank=True)
    nickname = serializers.CharField(required=False, allow_blank=True, default='')
    avatar_url = serializers.URLField(required=False, allow_blank=True, default='')
    openid = serializers.CharField(required=False, allow_blank=True, default='')

    def validate(self, attrs):
        if not attrs.get('code') and not attrs.get('openid'):
            raise serializers.ValidationError({'detail': '缺少微信登录 code'})
        return attrs
