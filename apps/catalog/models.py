from django.db import models


class PackageGroup(models.Model):
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
        return self.name


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
