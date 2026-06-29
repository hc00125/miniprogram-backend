from rest_framework import serializers

from .models import Addon, Package, PackageGroup, PackageSpec, PlayerType


class PackageGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = PackageGroup
        fields = ['id', 'name', 'sort_order', 'is_active']


class PackageSpecSerializer(serializers.ModelSerializer):
    class Meta:
        model = PackageSpec
        fields = [
            'id', 'package_id', 'name', 'short_name', 'display_name',
            'price', 'original_price',
            'description', 'guarantee_amount', 'sort_order', 'is_active',
        ]


class PackageSerializer(serializers.ModelSerializer):
    group_id = serializers.IntegerField(source='group.id', allow_null=True, read_only=True)
    group_name = serializers.CharField(source='group.name', allow_null=True, read_only=True)
    specs = serializers.SerializerMethodField()

    class Meta:
        model = Package
        fields = [
            'id', 'name', 'product_type', 'group_id', 'group_name',
            'player_count', 'base_price', 'original_price',
            'description', 'cover_url', 'image_url', 'thumb_url', 'picture_url',
            'gallery_images', 'detail_images', 'detail_text', 'rules_text',
            'sold_count', 'sort_order', 'is_active', 'is_custom', 'specs',
        ]

    def get_specs(self, obj):
        specs = obj.specs.filter(is_active=True).order_by('sort_order', 'id')
        return PackageSpecSerializer(specs, many=True).data


class PackageWriteSerializer(serializers.ModelSerializer):
    """管理员创建/编辑商品用，需要传 group_id"""
    group_id = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = Package
        fields = [
            'id', 'name', 'product_type', 'group_id',
            'player_count', 'base_price', 'original_price',
            'description', 'cover_url', 'image_url', 'thumb_url', 'picture_url',
            'gallery_images', 'detail_images', 'detail_text', 'rules_text',
            'sold_count', 'sort_order', 'is_active', 'is_custom',
        ]

    def create(self, validated_data):
        group_id = validated_data.pop('group_id', None)
        if group_id:
            validated_data['group'] = PackageGroup.objects.filter(id=group_id).first()
        return super().create(validated_data)

    def update(self, instance, validated_data):
        group_id = validated_data.pop('group_id', None)
        if group_id is not None:
            instance.group = PackageGroup.objects.filter(id=group_id).first()
        return super().update(instance, validated_data)


class AddonSerializer(serializers.ModelSerializer):
    class Meta:
        model = Addon
        fields = ['id', 'name', 'price_per_player', 'priority']


class PlayerTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlayerType
        fields = ['id', 'name', 'priority', 'price_extra']
