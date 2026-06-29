from django.contrib import admin

from .models import Addon, Package, PackageGroup, PackageSpec, PlayerType


class PackageSpecInline(admin.TabularInline):
    model = PackageSpec
    extra = 1
    fields = ['name', 'price', 'original_price', 'description', 'guarantee_amount', 'sort_order', 'is_active']
    ordering = ['sort_order', 'id']
    show_change_link = True


@admin.register(PackageGroup)
class PackageGroupAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'sort_order', 'is_active', 'created_at']
    list_editable = ['sort_order', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name']
    ordering = ['sort_order', 'id']
    list_per_page = 30


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'name', 'product_type', 'group', 'player_count', 'base_price',
        'sort_order', 'is_active', 'is_custom', 'sold_count', 'created_at',
    ]
    list_filter = ['group', 'product_type', 'is_active', 'is_custom']
    search_fields = ['name', 'description', 'rules_text']
    list_editable = ['player_count', 'base_price', 'sort_order', 'is_active', 'is_custom']
    list_select_related = ['group']
    ordering = ['sort_order', 'id']
    list_per_page = 30
    inlines = [PackageSpecInline]
    fieldsets = [
        ('基本信息', {
            'fields': ['name', 'product_type', 'group', 'player_count', 'base_price', 'original_price', 'description'],
        }),
        ('展示内容', {
            'fields': ['cover_url', 'image_url', 'thumb_url', 'picture_url', 'gallery_images', 'detail_images', 'detail_text', 'rules_text'],
        }),
        ('统计与排序', {
            'fields': ['sold_count', 'sort_order'],
        }),
        ('状态', {
            'fields': ['is_active', 'is_custom'],
        }),
    ]


@admin.register(PackageSpec)
class PackageSpecAdmin(admin.ModelAdmin):
    list_display = ['id', 'package', 'name', 'price', 'original_price', 'guarantee_amount', 'sort_order', 'is_active', 'created_at']
    list_filter = ['package', 'is_active']
    search_fields = ['name', 'display_name', 'short_name', 'package__name']
    list_editable = ['price', 'original_price', 'guarantee_amount', 'sort_order', 'is_active']
    list_select_related = ['package']
    ordering = ['package__sort_order', 'sort_order', 'id']
    list_per_page = 50


@admin.register(Addon)
class AddonAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'price_per_player', 'priority', 'is_active', 'created_at']
    list_filter = ['is_active']
    search_fields = ['name']
    list_editable = ['price_per_player', 'priority', 'is_active']
    ordering = ['priority', 'id']
    list_per_page = 30


@admin.register(PlayerType)
class PlayerTypeAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'priority', 'can_view_addon_priority', 'price_extra', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name']
    list_editable = ['priority', 'can_view_addon_priority', 'price_extra', 'is_active']
    ordering = ['priority', 'id']
    list_per_page = 30
