from django.contrib import admin
from .models import PatronageSettings, PlayerPatronageConfig, PatronagePurchase, CrownGrant, PatronageEarning


@admin.register(PatronageSettings)
class PatronageSettingsAdmin(admin.ModelAdmin):
    readonly_fields = ('updated_at',)
    def has_add_permission(self, request):
        return super().has_add_permission(request) and not PatronageSettings.objects.exists()
    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PlayerPatronageConfig)
class PlayerPatronageConfigAdmin(admin.ModelAdmin):
    list_display = ('player', 'hourly_rate_yuan', 'enabled', 'updated_at')
    search_fields = ('player__name',)
    autocomplete_fields = ('player',)
    readonly_fields = ('updated_at',)


class ReadOnlyRecordAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False
    def has_delete_permission(self, request, obj=None):
        return False
    def has_change_permission(self, request, obj=None):
        return False
    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


@admin.register(PatronagePurchase)
class PatronagePurchaseAdmin(ReadOnlyRecordAdmin):
    list_display = ('purchase_no', 'player_name', 'package_name', 'amount_yuan', 'payment_status', 'created_at')
    list_filter = ('payment_status', 'package_code')
    search_fields = ('purchase_no', 'player_name')


@admin.register(CrownGrant)
class CrownGrantAdmin(ReadOnlyRecordAdmin):
    list_display = ('purchase', 'package_name', 'source', 'starts_at', 'expires_at', 'revoked_at')
    list_filter = ('source',)
    search_fields = ('purchase__purchase_no', 'purchase__player_name')


@admin.register(PatronageEarning)
class PatronageEarningAdmin(ReadOnlyRecordAdmin):
    list_display = ('purchase', 'player', 'gross_amount', 'commission_amount', 'net_amount',
                    'debt_offset_amount', 'credited_amount', 'release_at')
    search_fields = ('purchase__purchase_no', 'purchase__player_name')
