from django.contrib import admin
from django.utils.html import format_html

from .models import GameService, Package, PackageGroup


def _insert_after(values, anchor, value):
    result = list(values or [])
    if value in result:
        return result
    try:
        index = result.index(anchor) + 1
    except ValueError:
        index = len(result)
    result.insert(index, value)
    return result


class GameServiceAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'icon_preview', 'name', 'code',
        'group_count', 'sort_order', 'is_active', 'updated_at',
    ]
    list_editable = ['sort_order', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name', 'code']
    ordering = ['sort_order', 'id']
    readonly_fields = ['icon_preview_large', 'created_at', 'updated_at']
    fieldsets = [
        ('游戏服务', {
            'fields': ['name', 'code', 'icon_preview_large', 'icon', 'icon_url'],
            'description': '上传方形图标即可，老板端会自动裁剪成圆形。上传图片优先于图标外链。',
        }),
        ('展示设置', {
            'fields': ['sort_order', 'is_active'],
        }),
        ('记录信息', {
            'fields': ['created_at', 'updated_at'],
        }),
    ]

    @admin.display(description='图标')
    def icon_preview(self, obj):
        url = obj.get_icon_url()
        if not url:
            return '未上传'
        return format_html(
            '<img src="{}" style="width:44px;height:44px;border-radius:50%;object-fit:cover;border:1px solid #ddd;" />',
            url,
        )

    @admin.display(description='图标预览')
    def icon_preview_large(self, obj):
        if not obj or not obj.pk:
            return '保存后显示预览'
        url = obj.get_icon_url()
        if not url:
            return '未上传图标'
        return format_html(
            '<img src="{}" style="width:96px;height:96px;border-radius:50%;object-fit:cover;border:1px solid #ddd;" />',
            url,
        )

    @admin.display(description='分类数')
    def group_count(self, obj):
        return obj.package_groups.count()


def patch_game_service_admin():
    if not admin.site.is_registered(GameService):
        admin.site.register(GameService, GameServiceAdmin)

    group_admin = admin.site._registry.get(PackageGroup)
    if group_admin:
        group_admin.list_display = _insert_after(group_admin.list_display, 'id', 'game_service')
        group_admin.list_filter = _insert_after(group_admin.list_filter, 'is_active', 'game_service')
        group_admin.search_fields = _insert_after(group_admin.search_fields, 'name', 'game_service__name')
        group_admin.list_select_related = ['game_service']

    package_admin = admin.site._registry.get(Package)
    if package_admin:
        package_admin.list_filter = _insert_after(package_admin.list_filter, 'group', 'group__game_service')
        package_admin.list_select_related = ['group', 'group__game_service']
