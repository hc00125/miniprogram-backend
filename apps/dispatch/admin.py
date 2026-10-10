from django.contrib import admin
from .models import Customer, DispatchReceipt, HistoryClaim


class AuditOnlyAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]
    def has_add_permission(self, request):
        return False
    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Customer)
class CustomerAdmin(AuditOnlyAdmin):
    list_display = ['id', 'nickname', 'user', 'created_by', 'created_at']
    search_fields = ['nickname', 'note']
    list_select_related = ['user', 'created_by']


@admin.register(DispatchReceipt)
class ReceiptAdmin(AuditOnlyAdmin):
    list_display = ['id', 'order', 'received_amount', 'created_by', 'created_at']
    search_fields = ['order__order_no', 'receipt_note']
    list_select_related = ['order', 'created_by']


@admin.register(HistoryClaim)
class ClaimAdmin(AuditOnlyAdmin):
    list_display = ['id', 'requested_number', 'applicant', 'status', 'reviewed_by', 'created_at']
    list_filter = ['status']
    list_select_related = ['applicant', 'reviewed_by']
