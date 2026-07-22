from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from io import BytesIO
from PIL import Image
from django.core.files.base import ContentFile


class GameService(models.Model):
    name = models.CharField(max_length=50, verbose_name='游戏名称')
    code = models.CharField(max_length=50, unique=True, verbose_name='游戏代码')
    icon = models.ImageField(
        upload_to='game-services/%Y/%m/',
        blank=True,
        null=True,
        verbose_name='游戏图标',
        help_text='建议上传256×256或512×512的方形PNG/JPG，前端会裁剪为圆形。',
    )
    icon_url = models.CharField(
        max_length=500,
        blank=True,
        default='',
        verbose_name='图标外链',
        help_text='可选。已在CDN/OSS上的图标可填写完整URL；上传图标优先。',
    )
    sort_order = models.IntegerField(default=0, verbose_name='排序')
    is_active = models.BooleanField(default=True, db_index=True, verbose_name='是否展示')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'game_services'
        verbose_name = '游戏服务'
        verbose_name_plural = '游戏服务'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return self.name

    def get_icon_url(self):
        if self.icon:
            return self.icon.url
        return self.icon_url or ''


class PackageGroup(models.Model):
    game_service = models.ForeignKey(
        GameService,
        on_delete=models.PROTECT,
        related_name='package_groups',
        verbose_name='所属游戏',
    )
    name = models.CharField(max_length=50)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'package_groups'
        verbose_name = '套餐分组'
        verbose_name_plural = '套餐分组列表'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return f'{self.game_service.name} - {self.name}'


class Package(models.Model):
    PRODUCT_TYPE_NORMAL = 'normal'
    PRODUCT_TYPE_GUARANTEE = 'guarantee'
    PRODUCT_TYPE_FUN = 'fun'
    PRODUCT_TYPE_SPECIAL = 'special'

    PRODUCT_TYPE_CHOICES = [
        (PRODUCT_TYPE_NORMAL, '普通商品'),
        (PRODUCT_TYPE_GUARANTEE, '保底单'),
        (PRODUCT_TYPE_FUN, '趣味单'),
        (PRODUCT_TYPE_SPECIAL, '特色单'),
    ]

    SELLING_MODE_PUBLIC = 'public'
    SELLING_MODE_PLAYER_DESIGNATED = 'player_designated'
    SELLING_MODE_CHOICES = [
        (SELLING_MODE_PUBLIC, '普通商城商品'),
        (SELLING_MODE_PLAYER_DESIGNATED, '陪玩师专属商品'),
    ]

    name = models.CharField(max_length=80, verbose_name='商品名称')
    product_type = models.CharField(
        max_length=30, choices=PRODUCT_TYPE_CHOICES,
        default=PRODUCT_TYPE_NORMAL, verbose_name='商品类型',
    )
    selling_mode = models.CharField(
        max_length=30,
        choices=SELLING_MODE_CHOICES,
        default=SELLING_MODE_PUBLIC,
        db_index=True,
        verbose_name='销售方式',
        help_text='陪玩师专属商品只能由所属陪玩师接受，不会进入公开抢单大厅。',
    )
    owner_player = models.ForeignKey(
        'players.Player',
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name='service_products',
        verbose_name='所属陪玩师',
        help_text='仅“陪玩师专属商品”需要填写；下单时由后端据此锁定服务人员。',
    )
    group = models.ForeignKey(
        PackageGroup, on_delete=models.SET_NULL,
        blank=True, null=True, related_name='packages',
        verbose_name='所属分类',
    )
    package_family = models.ForeignKey(
        'PackageFamily',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='packages',
        verbose_name='装备商品族',
        help_text='将同一装备配置的 1/2/3 人套餐归入同一个商品族，用于指定陪玩组合计价。',
    )
    base_price = models.FloatField(verbose_name='基础价')
    original_price = models.FloatField(blank=True, null=True, verbose_name='划线价')
    player_count = models.IntegerField(default=1, verbose_name='默认人数')
    requires_escort_qualification = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name='需要护航资格',
        help_text='开启后，该商品生成的订单只能由护航资格已通过的陪玩接单；娱乐陪、技术陪等等级规则仍同时生效。',
    )
    description = models.CharField(max_length=300, blank=True, null=True, verbose_name='商品简介')
    cover_url = models.CharField(max_length=500, blank=True, null=True, verbose_name='封面图URL')
    image_url = models.CharField(max_length=500, blank=True, null=True, verbose_name='商品图URL')
    thumb_url = models.CharField(max_length=500, blank=True, null=True, verbose_name='缩略图URL')
    picture_url = models.CharField(max_length=500, blank=True, null=True, verbose_name='展示图URL')
    gallery_images = models.JSONField(default=list, blank=True, verbose_name='轮播图列表')
    detail_images = models.JSONField(default=list, blank=True, verbose_name='图文详情长图列表')
    detail_text = models.TextField(blank=True, null=True, verbose_name='图文详情文字')
    rules_text = models.TextField(blank=True, null=True, verbose_name='规则/玩法说明')
    sold_count = models.IntegerField(default=0, verbose_name='已售数量')
    sort_order = models.IntegerField(default=0, verbose_name='排序')
    is_active = models.BooleanField(default=True, verbose_name='是否上架')
    is_custom = models.BooleanField(default=False, verbose_name='是否自定义')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'packages'
        verbose_name = '商品'
        verbose_name_plural = '商品列表'
        ordering = ['sort_order', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['package_family', 'player_count'],
                condition=Q(package_family__isnull=False),
                name='uniq_package_family_player_count',
            ),
            models.UniqueConstraint(
                fields=['owner_player'],
                condition=Q(selling_mode='player_designated'),
                name='uniq_player_designated_service_product',
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.selling_mode == self.SELLING_MODE_PLAYER_DESIGNATED:
            errors = {}
            if not self.owner_player_id:
                errors['owner_player'] = '陪玩师专属商品必须选择所属陪玩师。'
            if self.player_count != 1:
                errors['player_count'] = '陪玩师专属商品只能配置为 1 人。'
            if self.package_family_id:
                errors['package_family'] = '陪玩师专属商品不使用装备商品族。请将装备配置维护为商品规格。'
            if errors:
                raise ValidationError(errors)
        if not self.package_family_id:
            return

        errors = {}
        if self.player_count not in {1, 2, 3}:
            errors['player_count'] = '装备商品族内的套餐人数只能是 1、2 或 3 人。'

        duplicate = self.package_family.packages.filter(player_count=self.player_count)
        if self.pk:
            duplicate = duplicate.exclude(pk=self.pk)
        if duplicate.exists():
            errors['player_count'] = '同一装备商品族中，每个人数只能绑定一个套餐。'

        family_game_service_id = self.package_family.game_service_id
        package_game_service_id = getattr(self.group, 'game_service_id', None)
        if family_game_service_id and package_game_service_id and family_game_service_id != package_game_service_id:
            errors['package_family'] = '套餐所属游戏必须与装备商品族所属游戏一致。'

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # Existing ordinary packages remain fully backwards compatible.  Family
        # members opt into validation so API writes cannot create an ambiguous
        # 1/2/3-person equipment matrix.
        if self.package_family_id or self.selling_mode == self.SELLING_MODE_PLAYER_DESIGNATED:
            self.full_clean()
        return super().save(*args, **kwargs)


class PackageImage(models.Model):
    IMAGE_TYPE_COVER = 'cover'
    IMAGE_TYPE_GALLERY = 'gallery'
    IMAGE_TYPE_DETAIL = 'detail'

    IMAGE_TYPE_CHOICES = [
        (IMAGE_TYPE_COVER, '封面图'),
        (IMAGE_TYPE_GALLERY, '轮播图'),
        (IMAGE_TYPE_DETAIL, '详情长图'),
    ]

    package = models.ForeignKey(
        Package, on_delete=models.CASCADE,
        related_name='images', verbose_name='所属商品',
    )
    image_type = models.CharField(
        max_length=20, choices=IMAGE_TYPE_CHOICES,
        default=IMAGE_TYPE_GALLERY, verbose_name='图片类型',
    )
    image = models.ImageField(
        upload_to='packages/%Y/%m/', blank=True, null=True,
        verbose_name='上传图片',
    )
    external_url = models.CharField(
        max_length=500, blank=True, null=True,
        verbose_name='外链图片URL',
        help_text='可选。已经在 CDN/OSS 的图片可填这里；上传图片和外链二选一即可。',
    )
    sort_order = models.IntegerField(default=0, verbose_name='排序')
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'package_images'
        verbose_name = '商品图片'
        verbose_name_plural = '商品图片列表'
        ordering = ['image_type', 'sort_order', 'id']

    def __str__(self):
        return f'{self.package.name} - {self.get_image_type_display()}'

    def get_url(self):
        if self.image:
            return self.image.url
        return self.external_url or ''

    def save(self, *args, **kwargs):
        if self.image and self.image.name:
            img = Image.open(self.image)
            # 最大边长限制
            max_dim = 1920
            if max(img.width, img.height) > max_dim:
                ratio = max_dim / max(img.width, img.height)
                img = img.resize(
                    (int(img.width * ratio), int(img.height * ratio)),
                    Image.LANCZOS,
                )
            # 如果是 RGBA (PNG 透明)，保留原格式带优化
            # 否则转 JPEG 压缩
            buf = BytesIO()
            if img.mode in ('RGBA', 'LA', 'P'):
                img.save(buf, format='PNG', optimize=True)
                ext = '.png'
            else:
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img.save(buf, format='JPEG', quality=85, optimize=True)
                basename = self.image.name.rsplit('.', 1)[0]
                self.image.save(f'{basename}.jpg', ContentFile(buf.getvalue()), save=False)
                super().save(*args, **kwargs)
                return
            self.image.save(self.image.name, ContentFile(buf.getvalue()), save=False)
        super().save(*args, **kwargs)


class PackageSpec(models.Model):
    package = models.ForeignKey(
        Package, on_delete=models.CASCADE,
        related_name='specs', verbose_name='所属商品',
    )
    name = models.CharField(max_length=120, verbose_name='规格名称')
    short_name = models.CharField(max_length=60, blank=True, null=True, verbose_name='规格简称')
    display_name = models.CharField(max_length=60, blank=True, null=True, verbose_name='规格展示名')
    price = models.FloatField(verbose_name='规格价格')
    original_price = models.FloatField(blank=True, null=True, verbose_name='规格划线价')
    description = models.CharField(max_length=300, blank=True, null=True, verbose_name='规格说明')
    guarantee_amount = models.CharField(max_length=50, blank=True, null=True, verbose_name='保底金额')
    required_player_type = models.ForeignKey(
        'PlayerType',
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name='required_package_specs',
        verbose_name='最低陪玩等级',
        help_text='陪玩等级 priority 必须达到该类型或更高；留空表示不限制陪玩等级。',
    )
    sort_order = models.IntegerField(default=0, verbose_name='排序')
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'package_specs'
        verbose_name = '商品规格'
        verbose_name_plural = '商品规格列表'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return f'{self.package.name} - {self.name}'


class PackageFamily(models.Model):
    """同一装备配置下，不同人数的陪玩套餐集合。"""

    game_service = models.ForeignKey(
        GameService,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name='package_families',
        verbose_name='所属游戏',
    )
    name = models.CharField(max_length=80, verbose_name='装备商品族名称')
    code = models.SlugField(
        max_length=80,
        unique=True,
        verbose_name='装备商品族代码',
        help_text='稳定标识，例如 arena-four-sets-four-bullets。创建后不要随意修改。',
    )
    description = models.CharField(max_length=300, blank=True, default='', verbose_name='说明')
    sort_order = models.IntegerField(default=0, verbose_name='排序')
    is_active = models.BooleanField(default=True, db_index=True, verbose_name='是否启用')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'package_families'
        verbose_name = '装备商品族'
        verbose_name_plural = '装备商品族'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return self.name

    def package_for_player_count(self, player_count, *, active_only=True):
        """返回本装备下指定人数的唯一套餐；缺失时返回 ``None``。"""
        queryset = self.packages.filter(player_count=player_count)
        if active_only:
            queryset = queryset.filter(is_active=True)
        return queryset.order_by('sort_order', 'id').first()

    def package_matrix(self, *, active_only=True):
        """按人数返回 1/2/3 人套餐，供报价和后台完整度检查使用。"""
        queryset = self.packages.all()
        if active_only:
            queryset = queryset.filter(is_active=True)
        return {
            package.player_count: package
            for package in queryset.filter(player_count__in=[1, 2, 3]).order_by('player_count', 'sort_order', 'id')
        }


class PlayerOfferQuerySet(models.QuerySet):
    def active(self):
        """只保留后台启用且所属装备商品族启用的分配。"""
        return self.filter(is_active=True, package_family__is_active=True)

    def available(self):
        """可展示给指定流程的分配，不把陪玩师在线状态混入商品配置。"""
        return self.active().filter(
            player__status='approved',
            player__can_be_designated=True,
        )


class PlayerOffer(models.Model):
    """后台分配给陪玩师的可售装备商品族；不保存个人定价。"""

    player = models.ForeignKey(
        'players.Player',
        on_delete=models.CASCADE,
        related_name='player_offers',
        verbose_name='陪玩师',
    )
    package_family = models.ForeignKey(
        PackageFamily,
        on_delete=models.CASCADE,
        related_name='player_offers',
        verbose_name='装备商品族',
    )
    is_active = models.BooleanField(default=True, db_index=True, verbose_name='是否启用')
    sort_order = models.IntegerField(default=0, verbose_name='排序')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PlayerOfferQuerySet.as_manager()

    class Meta:
        db_table = 'player_offers'
        verbose_name = '陪玩可售装备'
        verbose_name_plural = '陪玩可售装备'
        ordering = ['sort_order', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['player', 'package_family'],
                name='uniq_player_package_family_offer',
            ),
        ]

    def __str__(self):
        return f'{self.player} - {self.package_family}'


class CompositionSku(models.Model):
    """指定阵容对应的静态结算 SKU，不在普通商城展示。"""

    package_family = models.ForeignKey(
        PackageFamily,
        on_delete=models.PROTECT,
        related_name='composition_skus',
        verbose_name='装备商品族',
    )
    base_player_type = models.ForeignKey(
        'PlayerType',
        on_delete=models.PROTECT,
        related_name='base_composition_skus',
        verbose_name='基础陪玩类型',
    )
    required_players = models.PositiveSmallIntegerField(verbose_name='总人数')
    designated_type_signature = models.JSONField(
        default=list,
        blank=True,
        verbose_name='指定计费类型签名',
        help_text='规范格式：[{' + '"player_type_id": 3, "count": 1' + '}]，保存时按类型 ID 排序并合并。',
    )
    composition_key = models.CharField(
        max_length=300,
        unique=True,
        db_index=True,
        editable=False,
        blank=True,
        verbose_name='组合键',
        help_text='由装备商品族、基础类型、总人数和指定类型签名自动生成。',
    )
    total_price_per_hour = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        verbose_name='组合每小时价格',
    )
    virtual_package_spec = models.OneToOneField(
        PackageSpec,
        on_delete=models.PROTECT,
        related_name='composition_virtual_sku',
        blank=True,
        null=True,
        verbose_name='内部虚拟支付规格',
        help_text='仅用于微信虚拟支付绑定的内部规格；其价格必须等于组合每小时价格。',
    )
    is_active = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name='是否启用',
        help_text='启用前必须补齐 1/2/3 人套餐、对应类型规格和微信虚拟支付绑定。',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'composition_skus'
        verbose_name = '指定陪玩组合 SKU'
        verbose_name_plural = '指定陪玩组合 SKU'
        ordering = ['package_family__sort_order', 'required_players', 'id']

    def __str__(self):
        return self.composition_key or f'{self.package_family} - 待生成组合键'

    @classmethod
    def build_composition_key(cls, package_family, base_player_type, required_players, designated_type_signature):
        from .composition import build_composition_key

        return build_composition_key(
            package_family=package_family,
            base_player_type=base_player_type,
            required_players=required_players,
            designated_type_signature=designated_type_signature,
        )

    @classmethod
    def resolve_for(
        cls,
        *,
        package_family,
        base_player_type,
        required_players,
        designated_type_signature,
        active_only=True,
    ):
        composition_key = cls.build_composition_key(
            package_family,
            base_player_type,
            required_players,
            designated_type_signature,
        )
        queryset = cls.objects.select_related(
            'package_family', 'base_player_type', 'virtual_package_spec',
        ).filter(composition_key=composition_key)
        if active_only:
            queryset = queryset.filter(is_active=True)
        return queryset.first()

    def active_virtual_binding(self):
        """返回价格匹配的微信虚拟支付绑定；未配置时返回 ``None``。"""
        if not self.virtual_package_spec_id:
            return None
        bindings = getattr(self.virtual_package_spec, 'virtual_payment_bindings', None)
        if bindings is None:
            return None
        expected_fen = int((Decimal(self.total_price_per_hour) * Decimal('100')).quantize(Decimal('1')))
        return bindings.filter(is_active=True, goods_price_fen=expected_fen).order_by('id').first()

    def clean(self):
        super().clean()
        from .composition import calculate_static_composition, canonicalize_designated_type_signature

        errors = {}
        total_price = None
        try:
            if self.total_price_per_hour is not None:
                total_price = Decimal(self.total_price_per_hour).quantize(Decimal('0.01'))
        except (TypeError, ValueError, ArithmeticError):
            errors['total_price_per_hour'] = '组合每小时价格必须是有效金额。'
        if self.required_players not in {1, 2, 3}:
            errors['required_players'] = '组合结算仅支持 1、2 或 3 人。'

        try:
            signature = canonicalize_designated_type_signature(self.designated_type_signature)
        except ValidationError as exc:
            errors['designated_type_signature'] = exc.messages
            signature = []
        else:
            self.designated_type_signature = signature

        if self.package_family_id and self.base_player_type_id and not errors:
            self.composition_key = self.build_composition_key(
                self.package_family,
                self.base_player_type,
                self.required_players,
                signature,
            )
            try:
                calculated = calculate_static_composition(
                    package_family=self.package_family,
                    base_player_type=self.base_player_type,
                    required_players=self.required_players,
                    designated_type_signature=signature,
                    active_only=False,
                )
            except ValidationError as exc:
                errors['designated_type_signature'] = exc.messages
            else:
                expected_price = calculated['total_price_per_hour']
                if total_price is not None and total_price != expected_price:
                    errors['total_price_per_hour'] = (
                        f'当前套餐规格静态计算结果为 ¥{expected_price:.2f}/小时，'
                        '组合 SKU 价格必须与其一致。'
                    )
                if self.is_active:
                    try:
                        calculate_static_composition(
                            package_family=self.package_family,
                            base_player_type=self.base_player_type,
                            required_players=self.required_players,
                            designated_type_signature=signature,
                            active_only=True,
                        )
                    except ValidationError as exc:
                        errors['is_active'] = exc.messages

        if self.virtual_package_spec_id:
            target_price = Decimal(str(self.virtual_package_spec.price or 0)).quantize(Decimal('0.01'))
            if total_price is not None and target_price != total_price:
                errors['virtual_package_spec'] = (
                    f'内部虚拟支付规格价格为 ¥{target_price:.2f}，'
                    f'必须等于组合价格 ¥{total_price:.2f}。'
                )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Addon(models.Model):
    name = models.CharField(max_length=50)
    price_per_player = models.FloatField()
    priority = models.IntegerField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'addons'
        verbose_name = '加价项目'
        verbose_name_plural = '加价项目列表'
        ordering = ['priority', 'id']

    def __str__(self):
        return self.name


class PlayerType(models.Model):
    name = models.CharField(max_length=20)
    priority = models.IntegerField()
    can_view_addon_priority = models.IntegerField(default=0)
    price_extra = models.FloatField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'player_types'
        verbose_name = '陪玩类型'
        verbose_name_plural = '陪玩类型列表'
        ordering = ['priority', 'id']

    def __str__(self):
        return self.name
