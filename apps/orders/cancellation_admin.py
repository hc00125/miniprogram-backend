from django.contrib import admin

from .cancellation_models import (
    OrderReplacementState,
    PlayerCancellationRecord,
    PlayerDiscipline,
)


class ReadOnlyAuditAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PlayerCancellationRecord)
class PlayerCancellationRecordAdmin(ReadOnlyAuditAdmin):
    list_display = [
        'id', 'order', 'player', 'stage', 'replacement_mode', 'was_designated',
        'used_free_chance', 'fine_rmb', 'fine_fish', 'deducted_fish', 'debt_fish',
        'suspended_until', 'created_at',
    ]
    list_filter = [
        'stage', 'replacement_mode', 'was_designated', 'used_free_chance', 'created_at',
    ]
    search_fields = [
        'order__order_no', 'player__name', 'reason', 'player_type_name_snapshot',
    ]
    readonly_fields = [
        'order', 'player', 'stage', 'replacement_mode', 'reason',
        'previous_order_status', 'was_designated', 'player_type_id_snapshot',
        'player_type_name_snapshot', 'booked_hours_snapshot', 'used_free_chance',
        'fine_rmb', 'fine_fish', 'deducted_fish', 'debt_fish', 'suspended_until',
        'wallet_adjustment', 'created_at',
    ]
    date_hierarchy = 'created_at'


@admin.register(PlayerDiscipline)
class PlayerDisciplineAdmin(admin.ModelAdmin):
    list_display = ['player', 'suspended_until', 'updated_at']
    search_fields = ['player__name']
    readonly_fields = ['player', 'updated_at']

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OrderReplacementState)
class OrderReplacementStateAdmin(ReadOnlyAuditAdmin):
    list_display = [
        'order', 'mode', 'status', 'missing_slots', 'cancelled_player_name',
        'required_player_type_name', 'remaining_minutes', 'current_designation',
        'updated_at',
    ]
    list_filter = ['mode', 'status', 'updated_at']
    search_fields = [
        'order__order_no', 'cancelled_player_name', 'required_player_type_name',
    ]
    readonly_fields = [
        'order', 'mode', 'status', 'missing_slots', 'resume_status',
        'remaining_minutes', 'cancelled_player_name', 'required_player_type_id',
        'required_player_type_name', 'latest_cancellation', 'current_designation',
        'resolved_at', 'created_at', 'updated_at',
    ]
