from rest_framework import serializers

from .models import Player, PlayerApplication


class PlayerSerializer(serializers.ModelSerializer):
    type_id = serializers.IntegerField(source='player_type_id', read_only=True)
    type_name = serializers.CharField(source='player_type.name', read_only=True)
    avg_rating = serializers.FloatField(read_only=True)

    class Meta:
        model = Player
        fields = ['id', 'name', 'type_id', 'type_name', 'contact_wechat', 'bio', 'is_online', 'total_orders', 'avg_rating']


class PlayerLoginSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=50)
    type_id = serializers.IntegerField()


class PlayerApplicationSerializer(serializers.ModelSerializer):
    nickname = serializers.CharField(source='user.client_profile.nickname', read_only=True)
    avatar_url = serializers.CharField(source='user.client_profile.avatar_url', read_only=True)

    class Meta:
        model = PlayerApplication
        fields = ['id', 'nickname', 'avatar_url', 'name', 'contact_wechat', 'bio', 'status', 'submitted_at', 'reviewed_at', 'reject_reason', 'remark']
        read_only_fields = ['status', 'submitted_at', 'reviewed_at', 'reject_reason', 'remark']


class PlayerApplicationCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlayerApplication
        fields = ['name', 'contact_wechat', 'bio']


class PlayerApplicationApproveSerializer(serializers.Serializer):
    player_type_id = serializers.IntegerField()
    remark = serializers.CharField(required=False, allow_blank=True, default='')


class PlayerApplicationRejectSerializer(serializers.Serializer):
    reject_reason = serializers.CharField(max_length=300)
