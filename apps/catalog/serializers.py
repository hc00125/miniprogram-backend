from rest_framework import serializers

from .models import Addon, Package, PackageGroup, PackageImage, PackageSpec, PlayerType


def build_absolute_image_url(request, url):
    if not url:
        return ''
    if url.startswith(('http://', 'https://')):
        return url
    if request:
        return request.build_absolute_uri(url)
    return url


def get_image_url(request, image):
    if not image:
        return ''
    return build_absolute_image_url(request, image.get_url())


def package_images(obj):
    images = getattr(obj, 'active_images', None)
    if images is None:
        images = obj.images.filter(is_active=True).order_by('image_type', 'sort_order', 'id')
    return list(images)


def image_urls_by_type(obj, request, image_type):
    return [
        get_image_url(request, image)
        for image in package_images(obj)
        if image.image_type == image_type and get_image_url(request, image)
    ]


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
    cover_url = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    thumb_url = serializers.SerializerMethodField()
    picture_url = serializers.SerializerMethodField()
    gallery_images = serializers.SerializerMethodField()
    detail_images = serializers.SerializerMethodField()
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

    @property
    def request(self):
        return self.context.get('request')

    def get_cover_url(self, obj):
        uploaded = image_urls_by_type(obj, self.request, PackageImage.IMAGE_TYPE_COVER)
        if uploaded:
            return uploaded[0]
        return build_absolute_image_url(self.request, obj.cover_url or '')

    def get_image_url(self, obj):
        return build_absolute_image_url(self.request, obj.image_url or '')

    def get_thumb_url(self, obj):
        return build_absolute_image_url(self.request, obj.thumb_url or '')

    def get_picture_url(self, obj):
        return build_absolute_image_url(self.request, obj.picture_url or '')

    def get_gallery_images(self, obj):
        uploaded = image_urls_by_type(obj, self.request, PackageImage.IMAGE_TYPE_GALLERY)
        legacy = [build_absolute_image_url(self.request, url) for url in (obj.gallery_images or []) if isinstance(url, str) and url]
        return uploaded + legacy

    def get_detail_images(self, obj):
        uploaded = image_urls_by_type(obj, self.request, PackageImage.IMAGE_TYPE_DETAIL)
        legacy = [build_absolute_image_url(self.request, url) for url in (obj.detail_images or []) if isinstance(url, str) and url]
        return uploaded + legacy

    def get_specs(self, obj):
        specs = getattr(obj, 'active_specs', None)
        if specs is None:
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
