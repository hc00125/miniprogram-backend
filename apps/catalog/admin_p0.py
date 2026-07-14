from django.contrib import admin

from .models import PackageSpec


@admin.register(PackageSpec)
class PackageSpecTypeAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'package', 'name', 'display_name', 'price',
        'required_player_type', 'sort_order', 'is_active',
    ]
    list_filter = ['required_player_type', 'is_active', 'package__group']
    search_fields = ['package__name', 'name', 'display_name', 'short_name']
    list_select_related = ['package', 'required_player_type']
    autocomplete_fields = ['package', 'required_player_type']
    list_editable = ['required_player_type', 'sort_order', 'is_active']
    ordering = ['package_id', 'sort_order', 'id']
