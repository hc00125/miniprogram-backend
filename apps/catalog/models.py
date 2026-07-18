from django.db import models
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

    name = models.CharField(max_length=80, verbose_name='商品名称')
    product_type = models.CharField(
        max_length=30, choices=PRODUCT_TYPE_CHOICES,
        default=PRODUCT_TYPE_NORMAL, verbose_name='商品类型',
    )
    group = models.ForeignKey(
        PackageGroup, on_delete=models.SET_NULL,
        blank=True, null=True, related_name='packages',
        verbose_name='所属分类',
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

    def __str__(self):
        return self.name


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
