from rest_framework import serializers

from .models import Addon, Package, PackageGroup, PlayerType


class PackageGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = PackageGroup
        fields = ['id', 'name', 'sort_order']


class PackageSerializer(serializers.ModelSerializer):
    group_id = serializers.IntegerField(source='group.id', allow_null=True, read_only=True)
    group_name = serializers.CharField(source='group.name', allow_null=True, read_only=True)

    class Meta:
        model = Package
        fields = ['id', 'name', 'player_count', 'base_price', 'description', 'is_custom', 'group_id', 'group_name']


class AddonSerializer(serializers.ModelSerializer):
    class Meta:
        model = Addon
        fields = ['id', 'name', 'price_per_player', 'priority']


class PlayerTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlayerType
        fields = ['id', 'name', 'priority', 'price_extra']
