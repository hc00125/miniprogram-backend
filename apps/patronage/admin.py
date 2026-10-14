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

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == 'hourly_rate_yuan':
            kwargs['help_text'] = '填写已批准的普通点单时薪，不填包天总价；按0.5元步长。包天 = 时薪 × 7小时；为空时禁售。计价不是履约计时。'
        return super().formfield_for_dbfield(db_field, request, **kwargs)


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
