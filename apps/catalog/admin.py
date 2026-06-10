from django.contrib import admin

from .models import Addon, Package, PackageGroup, PlayerType


@admin.register(PackageGroup)
class PackageGroupAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'sort_order', 'is_active']
    list_editable = ['sort_order', 'is_active']


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'player_count', 'base_price', 'group', 'is_active', 'is_custom']
    list_filter = ['group', 'is_active', 'is_custom']
    search_fields = ['name']


@admin.register(Addon)
class AddonAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'price_per_player', 'priority', 'is_active']
    list_editable = ['priority', 'is_active']


@admin.register(PlayerType)
class PlayerTypeAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'priority', 'can_view_addon_priority', 'price_extra', 'is_active']
    list_editable = ['priority', 'is_active']
