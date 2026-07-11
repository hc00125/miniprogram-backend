import re

from rest_framework import serializers

from apps.catalog.models import PlayerType

from .models import Player, PlayerApplication


class PlayerSerializer(serializers.ModelSerializer):
    type_id = serializers.IntegerField(source='player_type_id', read_only=True)
    type_name = serializers.CharField(source='player_type.name', read_only=True)
    avg_rating = serializers.FloatField(read_only=True)

    class Meta:
        model = Player
        fields = [
            'id', 'name', 'type_id', 'type_name', 'contact_wechat', 'bio',
            'audio_intro_url', 'audio_intro_title',
            'is_online', 'total_orders', 'avg_rating', 'rating_count'
        ]


class PlayerLoginSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=50)
    type_id = serializers.IntegerField()


class PlayerApplicationSerializer(serializers.ModelSerializer):
    nickname = serializers.CharField(source='user.client_profile.nickname', read_only=True)
    avatar_url = serializers.CharField(source='user.client_profile.avatar_url', read_only=True)
    type_id = serializers.IntegerField(source='player_type_id', read_only=True)
    type_name = serializers.CharField(source='player_type.name', read_only=True)

    class Meta:
        model = PlayerApplication
        fields = [
            'id', 'nickname', 'avatar_url', 'name', 'real_name', 'type_id', 'type_name',
            'contact_wechat', 'bio', 'audio_intro_url', 'audio_intro_title',
            'status', 'submitted_at', 'reviewed_at', 'reject_reason', 'remark'
        ]
        read_only_fields = ['status', 'submitted_at', 'reviewed_at', 'reject_reason', 'remark']


class PlayerApplicationCreateSerializer(serializers.ModelSerializer):
    type_id = serializers.IntegerField(write_only=True, required=False)
    player_type_id = serializers.IntegerField(write_only=True, required=False)
    real_name = serializers.CharField(required=True, allow_blank=False, max_length=30, trim_whitespace=True)
    audio_intro_url = serializers.CharField(required=False, allow_blank=True, default='')
    audio_intro_title = serializers.CharField(required=False, allow_blank=True, default='')

    class Meta:
        model = PlayerApplication
        fields = [
            'name', 'real_name', 'type_id', 'player_type_id', 'contact_wechat',
            'bio', 'audio_intro_url', 'audio_intro_title'
        ]

    def validate_real_name(self, value):
        normalized = re.sub(r'\s+', ' ', value.strip())
        visible_characters = normalized.replace(' ', '')
        if len(visible_characters) < 2:
            raise serializers.ValidationError('请输入完整真实姓名')
        if not re.fullmatch(r"[\u3400-\u9fffA-Za-z·•'’\- ]+", normalized):
            raise serializers.ValidationError('真实姓名只能包含中文、英文字母、中间点、空格、撇号或连字符')
        return normalized

    def validate(self, attrs):
        type_id = attrs.pop('type_id', None) or attrs.pop('player_type_id', None)
        if not type_id:
            raise serializers.ValidationError({'type_id': '请选择陪玩类型'})
        player_type = PlayerType.objects.filter(id=type_id, is_active=True).first()
        if not player_type:
            raise serializers.ValidationError({'type_id': '陪玩类型不存在'})
        attrs['player_type'] = player_type
        return attrs


class PlayerApplicationApproveSerializer(serializers.Serializer):
    player_type_id = serializers.IntegerField(required=False)
    remark = serializers.CharField(required=False, allow_blank=True, default='')


class PlayerApplicationRejectSerializer(serializers.Serializer):
    reject_reason = serializers.CharField(max_length=300)
