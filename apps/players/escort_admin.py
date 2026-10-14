from django.contrib import admin
from django.utils import timezone

from .escort_qualification import approve_escort_application, reject_escort_application
from .models import PlayerEscortApplication, PlayerEscortQualification


@admin.register(PlayerEscortQualification)
class PlayerEscortQualificationAdmin(admin.ModelAdmin):
    list_display = ['player', 'status', 'reviewed_at', 'reviewed_by', 'updated_at']
    list_filter = ['status', 'reviewed_at']
    search_fields = ['player__name', 'player__contact_wechat', 'review_note']
    readonly_fields = ['player', 'created_at', 'updated_at', 'reviewed_at', 'reviewed_by']
    fields = ['player', 'status', 'review_note', 'reviewed_at', 'reviewed_by', 'created_at', 'updated_at']

    def save_model(self, request, obj, form, change):
        if change and 'status' in form.changed_data:
            obj.reviewed_at = timezone.now()
            obj.reviewed_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(PlayerEscortApplication)
class PlayerEscortApplicationAdmin(admin.ModelAdmin):
    list_display = ['id', 'player', 'status', 'material_count', 'submitted_at', 'reviewed_at', 'reviewed_by']
    list_filter = ['status', 'submitted_at', 'reviewed_at']
    search_fields = ['player__name', 'player__contact_wechat', 'experience', 'reject_reason', 'review_note']
    actions = ['approve_applications', 'reject_applications']
    readonly_fields = ['player', 'experience', 'evidence_urls', 'submitted_at', 'reviewed_at', 'reviewed_by']
    fieldsets = (
        ('申请信息', {
            'fields': ('player', 'experience', 'evidence_urls', 'status'),
            'description': '护航资格独立于娱乐陪、技术陪等等级。请结合经历说明和证明材料审核。',
        }),
        ('审核信息', {
            'fields': ('reject_reason', 'review_note', 'submitted_at', 'reviewed_at', 'reviewed_by'),
        }),
    )

    @admin.display(description='材料数')
    def material_count(self, obj):
        return len(obj.evidence_urls or [])

    @admin.action(description='通过选中的护航资格申请')
    def approve_applications(self, request, queryset):
        count = 0
        for application in queryset.filter(status=PlayerEscortApplication.STATUS_PENDING):
            approve_escort_application(application, request.user, application.review_note)
            count += 1
        self.message_user(request, f'已通过 {count} 条护航资格申请')

    @admin.action(description='拒绝选中的护航资格申请')
    def reject_applications(self, request, queryset):
        count = 0
        for application in queryset.filter(status=PlayerEscortApplication.STATUS_PENDING):
            reason = application.reject_reason or '护航资格审核未通过，请补充材料后重新申请'
            reject_escort_application(application, reason, request.user, application.review_note)
            count += 1
        self.message_user(request, f'已拒绝 {count} 条护航资格申请')

    def save_model(self, request, obj, form, change):
        if not change or 'status' not in form.changed_data:
            super().save_model(request, obj, form, change)
            return
        requested_status = obj.status
        if requested_status == PlayerEscortApplication.STATUS_APPROVED:
            approve_escort_application(obj, request.user, obj.review_note)
            return
        if requested_status == PlayerEscortApplication.STATUS_REJECTED:
            reason = obj.reject_reason or '护航资格审核未通过，请补充材料后重新申请'
            reject_escort_application(obj, reason, request.user, obj.review_note)
            return
        super().save_model(request, obj, form, change)
