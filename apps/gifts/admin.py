from django.contrib import admin
from django.utils.html import format_html
from .models import Gift


@admin.register(Gift)
class GiftAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'kind', 'price_diamonds', 'is_active', 'sort_order', 'preview')
    list_filter = ('kind', 'is_active')
    search_fields = ('name', 'code')
    ordering = ('sort_order', 'id')
    readonly_fields = ('preview', 'created_at', 'updated_at')
    actions = None

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields + (('code', 'kind') if obj else ())

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='图片预览')
    def preview(self, obj):
        if obj and obj.image:
            return format_html('<img src="{}" alt="{}" width="64">', obj.image.url, obj.name)
        return '—'


from django import forms
from django.core.exceptions import ValidationError
from .models import PlayerGiftConfig, PlayerGiftConfigAudit
from .services.configuration import initialize_configs, update_config


class ConfigForm(forms.ModelForm):
    reason = forms.CharField(label='操作原因', max_length=300)
    expected_version = forms.IntegerField(widget=forms.HiddenInput, required=False)

    class Meta:
        model = PlayerGiftConfig
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['expected_version'].initial = self.instance.version

    def clean(self):
        data = super().clean()
        if self.instance.pk and data.get('expected_version') != self.instance.version:
            raise ValidationError('配置已变更，请刷新后确认')
        return data


@admin.register(PlayerGiftConfig)
class PlayerGiftConfigAdmin(admin.ModelAdmin):
    form = ConfigForm
    list_display = ('player', 'gift', 'gift_price', 'commission_rate', 'is_enabled', 'version')
    list_filter = ('is_enabled', 'gift', 'player')
    search_fields = ('player__name', 'gift__name', 'gift__code')
    readonly_fields = ('version', 'created_at', 'updated_at')
    actions = None

    @admin.display(description='钻石单价')
    def gift_price(self, obj):
        return obj.gift.price_diamonds

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields + (('player', 'gift') if obj else ())

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if not change:
            initialize_configs(actor=request.user, players=[obj.player], gifts=[obj.gift], reason=form.cleaned_data['reason'])
            saved = PlayerGiftConfig.objects.get(player=obj.player, gift=obj.gift)
            if saved.commission_rate != obj.commission_rate or saved.is_enabled != obj.is_enabled:
                saved = update_config(actor=request.user, config_id=saved.pk, commission_rate=obj.commission_rate,
                                      is_enabled=obj.is_enabled, reason=form.cleaned_data['reason'], expected_version=saved.version)
        else:
            saved = update_config(actor=request.user, config_id=obj.pk, commission_rate=obj.commission_rate,
                                  is_enabled=obj.is_enabled, reason=form.cleaned_data['reason'], expected_version=form.cleaned_data['expected_version'])
        obj.pk = saved.pk
        obj.version = saved.version
        obj._state = saved._state


class ReadOnlyAuditAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PlayerGiftConfigAudit)
class PlayerGiftConfigAuditAdmin(ReadOnlyAuditAdmin):
    list_display = ('id', 'config', 'actor', 'reason', 'created_at')


from .models import (GiftPurchase, GiftInventoryLot, GiftInventoryLedger, GiftTransfer,
                     GiftTransferAllocation, GiftEarning, GiftRefund)

for model in (GiftPurchase, GiftInventoryLot, GiftInventoryLedger, GiftTransfer,
              GiftTransferAllocation, GiftEarning, GiftRefund):
    admin.site.register(model, ReadOnlyAuditAdmin)
