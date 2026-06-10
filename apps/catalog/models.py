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
    name = models.CharField(max_length=50)
    player_count = models.IntegerField()
    base_price = models.FloatField()
    description = models.CharField(max_length=200, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    is_custom = models.BooleanField(default=False)
    group = models.ForeignKey(PackageGroup, on_delete=models.SET_NULL, blank=True, null=True, related_name='packages')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'packages'
        verbose_name = '套餐'
        verbose_name_plural = '套餐列表'
        ordering = ['group__sort_order', 'id']

    def __str__(self):
        return self.name


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