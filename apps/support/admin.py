from django.contrib import admin, messages
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect
from django.utils.decorators import method_decorator
from rest_framework.exceptions import APIException


class ComplaintHandlingForm(forms.Form):
    status = forms.ChoiceField(label='处理状态', choices=[
        ('pending', '待处理'), ('processing', '处理中'), ('awaiting_user', '等待用户补充'),
        ('resolved', '已解决'), ('closed', '已关闭')])
    reason = forms.CharField(label='内部状态变更说明（仅客服可见）', max_length=200, required=False)
    content = forms.CharField(label='回复或备注', max_length=1000, required=False, widget=forms.Textarea)
    visibility = forms.ChoiceField(label='消息可见范围', choices=[('internal', '内部备注（仅客服可见）'), ('public', '公开回复（投诉用户可见）')])
    claim = forms.BooleanField(label='由我接手处理', required=False)

    def clean(self):
        data = super().clean()
        if data.get('visibility') == 'public' and not data.get('content'):
            self.add_error('content', '公开回复不能为空')
        return data

from .models import SupportChannel, Complaint


@admin.register(Complaint)
class ComplaintAdmin(admin.ModelAdmin):
    list_display = ('number', 'user', 'order', 'category', 'status', 'assigned_to', 'created_at', 'updated_at')
    list_filter = ('status', 'category', 'created_at', 'assigned_to')
    search_fields = ('number', 'order__order_no', 'user__username')
    list_select_related = ('user', 'order', 'assigned_to')
    date_hierarchy = 'created_at'
    readonly_fields = ('number', 'user', 'order', 'category', 'description', 'contact',
                       'status', 'assigned_to', 'client_request_id', 'created_at', 'updated_at')
    fields = readonly_fields
    actions = None

    def get_urls(self):
        from django.urls import path
        return [path('<int:object_id>/attachments/<int:attachment_id>/',
            self.admin_site.admin_view(self.attachment_view),
            name='support_complaint_attachment')] + super().get_urls()

    def attachment_view(self, request, object_id, attachment_id):
        if not self.has_view_permission(request):
            raise PermissionDenied
        from django.shortcuts import get_object_or_404
        from django.http import FileResponse
        from .models import ComplaintAttachment
        from .services import private_storage
        attachment = get_object_or_404(ComplaintAttachment, pk=attachment_id, complaint_id=object_id)
        try:
            response = FileResponse(private_storage().open(attachment.storage_name, 'rb'),
                content_type=attachment.content_type, filename=attachment.name)
        except FileNotFoundError:
            raise Http404
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        response['Content-Security-Policy'] = "default-src 'none'; sandbox"
        return response

    @method_decorator(csrf_protect)
    def change_view(self, request, object_id, form_url='', extra_context=None):
        # Check permissions before reading any personal material, including forged POSTs.
        if not self.has_view_permission(request):
            raise PermissionDenied
        if request.method == 'POST' and (
                not self.has_change_permission(request) or request.POST.get('operation') != 'handle'):
            raise PermissionDenied
        obj = self.get_object(request, object_id)
        if obj is None:
            raise Http404
        form = ComplaintHandlingForm(request.POST if request.method == 'POST' else None,
                                     initial={'status': obj.status, 'visibility': 'internal'})
        if request.method == 'POST' and form.is_valid():
            try:
                self.handle_complaint(request, obj.pk, form.cleaned_data)
            except (ValidationError, APIException) as exc:
                form.add_error(None, str(exc))
            else:
                self.message_user(request, '处理记录已保存；未执行退款、扣款或账号处罚。', messages.SUCCESS)
                return HttpResponseRedirect(reverse('admin:support_complaint_change', args=[obj.pk]))
        context = {
            **self.admin_site.each_context(request), 'opts': self.model._meta,
            'title': '投诉工单处理', 'original': obj, 'complaint': obj, 'form': form,
            'can_handle': self.has_change_permission(request, obj) and obj.status != 'closed',
            'ticket_messages': obj.messages.select_related('author').order_by('created_at', 'pk')
                if hasattr(obj, 'messages') else [],
            'status_history': obj.status_history.select_related('operator').order_by('created_at', 'pk')
                if hasattr(obj, 'status_history') else [],
            'attachments': obj.attachments.all() if hasattr(obj, 'attachments') else [],
            'handling_history': self.get_admin_history(obj),
        }
        return TemplateResponse(request, 'admin/support/complaint_detail.html', context)

    def get_admin_history(self, obj):
        from django.contrib.admin.models import LogEntry
        from django.contrib.contenttypes.models import ContentType
        return LogEntry.objects.filter(content_type=ContentType.objects.get_for_model(obj),
            object_id=str(obj.pk)).select_related('user').order_by('action_time', 'pk')

    @transaction.atomic
    def handle_complaint(self, request, pk, data):
        if not self.has_change_permission(request):
            raise PermissionDenied
        obj = Complaint.objects.select_for_update().get(pk=pk)
        if obj.status == 'closed':
            raise ValidationError('工单已关闭，不能修改原始材料或追加处理。')
        # Shared service validates transitions; status/time are public, reason is internal.
        # Required contract: transition_complaint(complaint, status, actor, reason).
        from .services import transition_complaint
        from .models import ComplaintMessage
        audit = []
        if data['status'] != obj.status:
            if not data['reason']:
                raise ValidationError('变更状态时请填写内部状态变更说明（仅客服可见）。')
            old_status = obj.status
            transition_complaint(obj, data['status'], request.user, data['reason'])
            audit.append(f'状态：{old_status} → {data["status"]}；{data["reason"]}')
        if data['claim'] and obj.assigned_to_id != request.user.pk:
            old_assignee = obj.assigned_to_id
            obj.assigned_to = request.user
            obj.save(update_fields=['assigned_to', 'updated_at'])
            audit.append(f'处理人：{old_assignee or "未分配"} → {request.user.pk}')
        if data['content']:
            internal = data['visibility'] == 'internal'
            msg = ComplaintMessage.objects.create(complaint=obj, author=request.user,
                content=data['content'], is_internal=internal)
            obj.save(update_fields=['updated_at'])
            audit.append(f'{"内部备注" if internal else "公开回复"} #{msg.pk}')
        if not audit:
            raise ValidationError('请变更状态、接手工单或填写回复/备注。')
        self.log_change(request, obj, '；'.join(audit))

    def has_module_permission(self, request):
        return self.has_view_permission(request)

    def has_view_permission(self, request, obj=None):
        return bool(request.user.is_active and request.user.is_staff and (
            request.user.has_perm('support.view_complaint') or
            request.user.has_perm('support.change_complaint')))

    def has_change_permission(self, request, obj=None):
        return bool(request.user.is_active and request.user.is_staff and
                    request.user.has_perm('support.change_complaint'))

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SupportChannel)
class SupportChannelAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'channel_type', 'wechat_id', 'audience',
        'service_hours', 'sort_order', 'is_active', 'updated_at',
    ]
    list_editable = ['sort_order', 'is_active']
    list_filter = ['channel_type', 'audience', 'is_active']
    search_fields = ['name', 'wechat_id', 'description', 'service_hours']
    ordering = ['sort_order', 'id']
    readonly_fields = ['created_at', 'updated_at']
    fieldsets = [
        ('基础信息', {
            'fields': ['name', 'channel_type', 'wechat_id', 'description'],
        }),
        ('展示规则', {
            'fields': ['audience', 'service_hours', 'sort_order', 'is_active'],
        }),
        ('记录信息', {
            'fields': ['created_at', 'updated_at'],
        }),
    ]

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False
