import re

from rest_framework import serializers

from apps.catalog.models import PlayerType

from .models import (
    Player,
    PlayerApplication,
    PlayerEscortApplication,
    PlayerEscortQualification,
    PlayerProfileUpdateRequest,
)


class PlayerSerializer(serializers.ModelSerializer):
    type_id = serializers.IntegerField(source='player_type_id', read_only=True)
    type_name = serializers.CharField(source='player_type.name', read_only=True)
    avg_rating = serializers.FloatField(read_only=True)
    escort_status = serializers.SerializerMethodField()
    escort_status_text = serializers.SerializerMethodField()
    has_escort_qualification = serializers.SerializerMethodField()

    class Meta:
        model = Player
        fields = [
            'id', 'name', 'type_id', 'type_name', 'contact_wechat', 'bio',
            'audio_intro_url', 'audio_intro_title',
            'is_online', 'total_orders', 'avg_rating', 'rating_count',
            'can_accept_orders', 'can_be_designated', 'is_publicly_visible', 'can_withdraw',
            'escort_status', 'escort_status_text', 'has_escort_qualification',
        ]

    def get_escort_status(self, obj):
        qualification = getattr(obj, 'escort_qualification', None)
        return qualification.status if qualification else PlayerEscortQualification.STATUS_NONE

    def get_escort_status_text(self, obj):
        qualification = getattr(obj, 'escort_qualification', None)
        return qualification.get_status_display() if qualification else '未申请'

    def get_has_escort_qualification(self, obj):
        qualification = getattr(obj, 'escort_qualification', None)
        return bool(qualification and qualification.status == PlayerEscortQualification.STATUS_APPROVED)


class PlayerProfileUpdateRequestSerializer(serializers.ModelSerializer):
    status_text = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = PlayerProfileUpdateRequest
        fields = [
            'id', 'bio', 'audio_intro_url', 'audio_intro_title', 'status',
            'status_text', 'reject_reason', 'submitted_at', 'reviewed_at',
        ]
        read_only_fields = [
            'id', 'status', 'status_text', 'reject_reason', 'submitted_at', 'reviewed_at',
        ]


class PlayerProfileUpdateCreateSerializer(serializers.Serializer):
    bio = serializers.CharField(required=False, allow_blank=True, max_length=500)
    audio_intro_url = serializers.CharField(required=False, allow_blank=True, max_length=500)
    audio_intro_title = serializers.CharField(required=False, allow_blank=True, max_length=100)

    def validate(self, attrs):
        attrs['bio'] = (attrs.get('bio') or '').strip()
        attrs['audio_intro_url'] = (attrs.get('audio_intro_url') or '').strip()
        attrs['audio_intro_title'] = (attrs.get('audio_intro_title') or '').strip()
        if attrs['audio_intro_url'] and not attrs['audio_intro_title']:
            attrs['audio_intro_title'] = '音频自我介绍'
        return attrs


class PlayerEscortApplicationSerializer(serializers.ModelSerializer):
    status_text = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = PlayerEscortApplication
        fields = [
            'id', 'experience', 'evidence_urls', 'status', 'status_text',
            'reject_reason', 'review_note', 'submitted_at', 'reviewed_at',
        ]
        read_only_fields = [
            'id', 'status', 'status_text', 'reject_reason', 'review_note',
            'submitted_at', 'reviewed_at',
        ]


class PlayerEscortApplicationCreateSerializer(serializers.Serializer):
    experience = serializers.CharField(min_length=10, max_length=1000, trim_whitespace=True)
    evidence_urls = serializers.ListField(
        child=serializers.URLField(max_length=500),
        required=False,
        default=list,
        max_length=5,
    )

    def validate_experience(self, value):
        value = value.strip()
        if len(value) < 10:
            raise serializers.ValidationError('请至少填写10个字的护航经历与能力说明')
        return value


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
