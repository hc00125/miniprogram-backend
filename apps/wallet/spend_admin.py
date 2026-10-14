from django.contrib import admin
from .spend_models import WalletSpendAttempt, WalletSpendAudit, OrderWalletSpend


class ReadOnlySpendAdmin(admin.ModelAdmin):
    actions = None
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False
    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


@admin.register(WalletSpendAttempt)
class SpendAttemptAdmin(ReadOnlySpendAdmin):
    list_display = ('id', 'kind', 'business_no', 'status', 'amount', 'reserved_amount', 'blocker', 'created_at')
    list_filter = ('status', 'kind', 'blocker')
    search_fields = ('external_id', 'business_no')


@admin.register(WalletSpendAudit)
class SpendAuditAdmin(ReadOnlySpendAdmin):
    list_display = ('id', 'attempt_id', 'event', 'created_at')
    list_filter = ('event',)


@admin.register(OrderWalletSpend)
class OrderWalletSpendAdmin(ReadOnlySpendAdmin):
    list_display = ('id', 'order_id', 'attempt_id', 'blocker', 'created_at')
    list_filter = ('blocker',)
    search_fields = ('order__order_no', 'attempt__external_id')
