from rest_framework import serializers

from .models import (
    Addon,
    CompositionSku,
    GameService,
    Package,
    PackageFamily,
    PackageGroup,
    PackageImage,
    PackageSpec,
    PlayerOffer,
    PlayerType,
)


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
    game_service_id = serializers.IntegerField(read_only=True)
    game_service_name = serializers.CharField(source='game_service.name', read_only=True)

    class Meta:
        model = PackageGroup
        fields = [
            'id', 'name', 'sort_order', 'is_active',
            'game_service_id', 'game_service_name',
        ]


class GameServiceGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = PackageGroup
        fields = ['id', 'name', 'sort_order']


class GameServiceSerializer(serializers.ModelSerializer):
    icon_url = serializers.SerializerMethodField()
    groups = serializers.SerializerMethodField()

    class Meta:
        model = GameService
        fields = ['id', 'name', 'code', 'icon_url', 'sort_order', 'groups']

    def get_icon_url(self, obj):
        return build_absolute_image_url(self.context.get('request'), obj.get_icon_url())

    def get_groups(self, obj):
        groups = getattr(obj, 'active_groups', None)
        if groups is None:
            groups = obj.package_groups.filter(is_active=True).order_by('sort_order', 'id')
        return GameServiceGroupSerializer(groups, many=True).data


class PackageSpecSerializer(serializers.ModelSerializer):
    required_player_type_id = serializers.IntegerField(read_only=True, allow_null=True)
    required_player_type_name = serializers.SerializerMethodField()
    required_player_type_priority = serializers.SerializerMethodField()

    class Meta:
        model = PackageSpec
        fields = [
            'id', 'package_id', 'name', 'short_name', 'display_name',
            'price', 'original_price',
            'description', 'guarantee_amount',
            'required_player_type_id', 'required_player_type_name', 'required_player_type_priority',
            'sort_order', 'is_active',
        ]

    def get_required_player_type_name(self, obj):
        return obj.required_player_type.name if obj.required_player_type_id else None

    def get_required_player_type_priority(self, obj):
        return obj.required_player_type.priority if obj.required_player_type_id else None


class PackageSerializer(serializers.ModelSerializer):
    group_id = serializers.IntegerField(source='group.id', allow_null=True, read_only=True)
    group_name = serializers.CharField(source='group.name', allow_null=True, read_only=True)
    game_service_id = serializers.IntegerField(source='group.game_service.id', allow_null=True, read_only=True)
    game_service_name = serializers.CharField(source='group.game_service.name', allow_null=True, read_only=True)
    package_family_id = serializers.IntegerField(read_only=True, allow_null=True)
    package_family_code = serializers.CharField(source='package_family.code', allow_null=True, read_only=True)
    package_family_name = serializers.CharField(source='package_family.name', allow_null=True, read_only=True)
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
            'game_service_id', 'game_service_name',
            'package_family_id', 'package_family_code', 'package_family_name',
            'player_count', 'base_price', 'original_price', 'requires_escort_qualification',
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
            specs = obj.specs.filter(is_active=True).select_related('required_player_type').order_by('sort_order', 'id')
        return PackageSpecSerializer(specs, many=True).data


class PackageWriteSerializer(serializers.ModelSerializer):
    """管理员创建/编辑商品用，需要传 group_id"""
    group_id = serializers.IntegerField(required=False, allow_null=True)
    package_family_id = serializers.IntegerField(required=False, allow_null=True)
    _missing = object()

    class Meta:
        model = Package
        fields = [
            'id', 'name', 'product_type', 'group_id', 'package_family_id',
            'player_count', 'base_price', 'original_price', 'requires_escort_qualification',
            'description', 'cover_url', 'image_url', 'thumb_url', 'picture_url',
            'gallery_images', 'detail_images', 'detail_text', 'rules_text',
            'sold_count', 'sort_order', 'is_active', 'is_custom',
        ]

    def resolve_group(self, group_id):
        if group_id is None:
            return None
        try:
            return PackageGroup.objects.get(pk=group_id)
        except PackageGroup.DoesNotExist as exc:
            raise serializers.ValidationError({'group_id': '套餐分组不存在'}) from exc

    def resolve_package_family(self, package_family_id):
        if package_family_id is None:
            return None
        try:
            return PackageFamily.objects.get(pk=package_family_id)
        except PackageFamily.DoesNotExist as exc:
            raise serializers.ValidationError({'package_family_id': '装备商品族不存在'}) from exc

    def create(self, validated_data):
        group_id = validated_data.pop('group_id', self._missing)
        package_family_id = validated_data.pop('package_family_id', self._missing)
        if group_id is not self._missing:
            validated_data['group'] = self.resolve_group(group_id)
        if package_family_id is not self._missing:
            validated_data['package_family'] = self.resolve_package_family(package_family_id)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        group_id = validated_data.pop('group_id', self._missing)
        package_family_id = validated_data.pop('package_family_id', self._missing)
        if group_id is not self._missing:
            validated_data['group'] = self.resolve_group(group_id)
        if package_family_id is not self._missing:
            validated_data['package_family'] = self.resolve_package_family(package_family_id)
        return super().update(instance, validated_data)


class AddonSerializer(serializers.ModelSerializer):
    class Meta:
        model = Addon
        fields = '__all__'


class PlayerTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlayerType
        fields = '__all__'


class PackageFamilySerializer(serializers.ModelSerializer):
    game_service_id = serializers.IntegerField(read_only=True, allow_null=True)
    game_service_name = serializers.CharField(source='game_service.name', read_only=True, allow_null=True)
    packages = serializers.SerializerMethodField()

    class Meta:
        model = PackageFamily
        fields = [
            'id', 'code', 'name', 'description', 'sort_order', 'is_active',
            'game_service_id', 'game_service_name', 'packages',
        ]

    def get_packages(self, obj):
        packages = getattr(obj, 'active_packages', None)
        if packages is None:
            packages = obj.packages.filter(is_active=True).select_related(
                'group', 'group__game_service', 'package_family',
            ).prefetch_related('specs__required_player_type').order_by('player_count', 'sort_order', 'id')
        return PackageSerializer(packages, many=True, context=self.context).data


class PackageFamilyWriteSerializer(serializers.ModelSerializer):
    game_service_id = serializers.IntegerField(required=False, allow_null=True)
    _missing = object()

    class Meta:
        model = PackageFamily
        fields = [
            'id', 'code', 'name', 'description', 'sort_order', 'is_active', 'game_service_id',
        ]

    def _resolve_game_service(self, game_service_id):
        if game_service_id is None:
            return None
        try:
            return GameService.objects.get(pk=game_service_id)
        except GameService.DoesNotExist as exc:
            raise serializers.ValidationError({'game_service_id': '游戏服务不存在'}) from exc

    def create(self, validated_data):
        game_service_id = validated_data.pop('game_service_id', self._missing)
        if game_service_id is not self._missing:
            validated_data['game_service'] = self._resolve_game_service(game_service_id)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        game_service_id = validated_data.pop('game_service_id', self._missing)
        if game_service_id is not self._missing:
            validated_data['game_service'] = self._resolve_game_service(game_service_id)
        return super().update(instance, validated_data)


class PlayerOfferSerializer(serializers.ModelSerializer):
    player_id = serializers.IntegerField(read_only=True)
    player_name = serializers.CharField(source='player.name', read_only=True)
    player_type_id = serializers.IntegerField(source='player.player_type_id', read_only=True)
    player_type_name = serializers.CharField(source='player.player_type.name', read_only=True)
    designated_billing_type_id = serializers.SerializerMethodField()
    designated_billing_type_name = serializers.SerializerMethodField()
    designated_billing_type_priority = serializers.SerializerMethodField()
    package_family = PackageFamilySerializer(read_only=True)
    is_available = serializers.SerializerMethodField()

    class Meta:
        model = PlayerOffer
        fields = [
            'id', 'player_id', 'player_name', 'player_type_id', 'player_type_name',
            'designated_billing_type_id', 'designated_billing_type_name', 'designated_billing_type_priority',
            'package_family', 'is_active', 'is_available', 'sort_order',
        ]

    @staticmethod
    def _billing_type(obj):
        return obj.player.minimum_designated_player_type or obj.player.player_type

    def get_designated_billing_type_id(self, obj):
        billing_type = self._billing_type(obj)
        return billing_type.id if billing_type else None

    def get_designated_billing_type_name(self, obj):
        billing_type = self._billing_type(obj)
        return billing_type.name if billing_type else ''

    def get_designated_billing_type_priority(self, obj):
        billing_type = self._billing_type(obj)
        return billing_type.priority if billing_type else 0

    def get_is_available(self, obj):
        player = obj.player
        return bool(
            obj.is_active
            and obj.package_family.is_active
            and player.status == 'approved'
            and player.can_be_designated
        )


class PlayerOfferWriteSerializer(serializers.ModelSerializer):
    player_id = serializers.IntegerField(write_only=True)
    package_family_id = serializers.IntegerField(write_only=True)

    class Meta:
        model = PlayerOffer
        fields = ['id', 'player_id', 'package_family_id', 'is_active', 'sort_order']

    def validate_player_id(self, value):
        from apps.players.models import Player

        if not Player.objects.filter(pk=value).exists():
            raise serializers.ValidationError('陪玩师不存在')
        return value

    def validate_package_family_id(self, value):
        if not PackageFamily.objects.filter(pk=value).exists():
            raise serializers.ValidationError('装备商品族不存在')
        return value

    def create(self, validated_data):
        from apps.players.models import Player

        player_id = validated_data.pop('player_id')
        package_family_id = validated_data.pop('package_family_id')
        return PlayerOffer.objects.create(
            player=Player.objects.get(pk=player_id),
            package_family=PackageFamily.objects.get(pk=package_family_id),
            **validated_data,
        )

    def update(self, instance, validated_data):
        from apps.players.models import Player

        if 'player_id' in validated_data:
            instance.player = Player.objects.get(pk=validated_data.pop('player_id'))
        if 'package_family_id' in validated_data:
            instance.package_family = PackageFamily.objects.get(pk=validated_data.pop('package_family_id'))
        return super().update(instance, validated_data)


class CompositionSkuSerializer(serializers.ModelSerializer):
    package_family_id = serializers.IntegerField(read_only=True)
    package_family_code = serializers.CharField(source='package_family.code', read_only=True)
    package_family_name = serializers.CharField(source='package_family.name', read_only=True)
    base_player_type_id = serializers.IntegerField(read_only=True)
    base_player_type_name = serializers.CharField(source='base_player_type.name', read_only=True)
    base_player_type_priority = serializers.IntegerField(source='base_player_type.priority', read_only=True)
    virtual_package_spec_id = serializers.IntegerField(read_only=True, allow_null=True)
    virtual_payment = serializers.SerializerMethodField()

    class Meta:
        model = CompositionSku
        fields = [
            'id', 'composition_key',
            'package_family_id', 'package_family_code', 'package_family_name',
            'base_player_type_id', 'base_player_type_name', 'base_player_type_priority',
            'required_players', 'designated_type_signature', 'total_price_per_hour',
            'virtual_package_spec_id', 'virtual_payment', 'is_active',
        ]

    def get_virtual_payment(self, obj):
        binding = obj.active_virtual_binding()
        expected_fen = int(obj.total_price_per_hour * 100)
        return {
            'ready': bool(binding),
            'expected_goods_price_fen': expected_fen,
            'product_id': binding.product_id if binding else '',
            'goods_price_fen': binding.goods_price_fen if binding else None,
        }


class CompositionSkuWriteSerializer(serializers.ModelSerializer):
    package_family_id = serializers.IntegerField(write_only=True)
    base_player_type_id = serializers.IntegerField(write_only=True)
    virtual_package_spec_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)

    class Meta:
        model = CompositionSku
        fields = [
            'id', 'package_family_id', 'base_player_type_id', 'required_players',
            'designated_type_signature', 'total_price_per_hour',
            'virtual_package_spec_id', 'is_active',
        ]

    def validate_package_family_id(self, value):
        if not PackageFamily.objects.filter(pk=value).exists():
            raise serializers.ValidationError('装备商品族不存在')
        return value

    def validate_base_player_type_id(self, value):
        if not PlayerType.objects.filter(pk=value).exists():
            raise serializers.ValidationError('基础陪玩类型不存在')
        return value

    def validate_virtual_package_spec_id(self, value):
        if value is not None and not PackageSpec.objects.filter(pk=value).exists():
            raise serializers.ValidationError('内部虚拟支付规格不存在')
        return value

    def _resolve_relations(self, validated_data):
        package_family_id = validated_data.pop('package_family_id', None)
        base_player_type_id = validated_data.pop('base_player_type_id', None)
        virtual_package_spec_id = validated_data.pop('virtual_package_spec_id', serializers.empty)
        if package_family_id is not None:
            validated_data['package_family'] = PackageFamily.objects.get(pk=package_family_id)
        if base_player_type_id is not None:
            validated_data['base_player_type'] = PlayerType.objects.get(pk=base_player_type_id)
        if virtual_package_spec_id is not serializers.empty:
            validated_data['virtual_package_spec'] = (
                PackageSpec.objects.get(pk=virtual_package_spec_id)
                if virtual_package_spec_id is not None else None
            )
        return validated_data

    def create(self, validated_data):
        return super().create(self._resolve_relations(validated_data))

    def update(self, instance, validated_data):
        return super().update(instance, self._resolve_relations(validated_data))
