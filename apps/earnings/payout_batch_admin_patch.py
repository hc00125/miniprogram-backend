from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.template.response import TemplateResponse
from rest_framework.exceptions import ValidationError

from . import admin as earnings_admin  # noqa: F401  确保原提现后台已注册
from .models import Withdrawal
from .payout_batch_models import WithdrawalPayoutBatch, WithdrawalPayoutBatchItem
from .payout_batches import approve_withdrawals_batch, mark_withdrawals_paid_batch


class BatchApproveForm(forms.Form):
    admin_note = forms.CharField(
        label='审核备注',
        required=False,
        max_length=500,
        widget=forms.Textarea(attrs={'rows': 3, 'placeholder': '选填；会统一写入本次选中的提现申请'}),
    )


class BatchPayoutForm(forms.Form):
    payment_method = forms.ChoiceField(label='实际付款方式', choices=Withdrawal.METHOD_CHOICES)
    external_transfer_no = forms.CharField(
        label='外部转账批次号',
        required=False,
        max_length=150,
        help_text='选填。微信、支付宝或银行提供的流水号；不填写也可以完成付款。',
    )
    admin_note = forms.CharField(
        label='付款备注',
        required=False,
        max_length=500,
        widget=forms.Textarea(attrs={'rows': 3, 'placeholder': '选填；例如付款日期、异常说明或财务备注'}),
    )
    proof_url = forms.CharField(
        label='付款凭证URL',
        required=False,
        max_length=500,
        help_text='选填。可填写统一付款截图或凭证地址。',
    )


class WithdrawalPayoutBatchItemInline(admin.TabularInline):
    model = WithdrawalPayoutBatchItem
    extra = 0
    can_delete = False
    fields = ['withdrawal', 'player_name', 'amount_snapshot', 'created_at']
    readonly_fields = fields

    @admin.display(description='陪玩')
    def player_name(self, obj):
        return obj.withdrawal.player.name

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(WithdrawalPayoutBatch)
class WithdrawalPayoutBatchAdmin(admin.ModelAdmin):
    list_display = [
        'batch_no', 'payment_method', 'item_count', 'total_amount',
        'external_transfer_no', 'created_by', 'created_at',
    ]
    list_filter = ['payment_method', 'created_at']
    search_fields = [
        'batch_no', 'external_transfer_no', 'admin_note',
        'items__withdrawal__withdrawal_no', 'items__withdrawal__player__name',
    ]
    readonly_fields = [
        'batch_no', 'payment_method', 'item_count', 'total_amount',
        'external_transfer_no', 'admin_note', 'proof_url', 'created_by', 'created_at',
    ]
    fields = readonly_fields
    inlines = [WithdrawalPayoutBatchItemInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in {'GET', 'HEAD', 'OPTIONS'}

    def has_delete_permission(self, request, obj=None):
        return False


def _render_batch_action(model_admin, request, queryset, form, *, title, action_name, submit_label):
    total_amount = sum((item.amount for item in queryset), 0)
    context = {
        **model_admin.admin_site.each_context(request),
        'title': title,
        'opts': model_admin.model._meta,
        'queryset': queryset,
        'form': form,
        'action_name': action_name,
        'action_checkbox_name': ACTION_CHECKBOX_NAME,
        'submit_label': submit_label,
        'selected_count': queryset.count(),
        'total_amount': total_amount,
        'media': model_admin.media + form.media,
    }
    request.current_app = model_admin.admin_site.name
    return TemplateResponse(
        request,
        'admin/earnings/withdrawal/batch_action.html',
        context,
    )


@admin.action(description='批量审核通过，进入待打款（备注选填）')
def approve_selected(self, request, queryset):
    if request.POST.get('confirm_batch_action') == '1':
        form = BatchApproveForm(request.POST)
        if form.is_valid():
            try:
                approved = approve_withdrawals_batch(
                    queryset.values_list('pk', flat=True),
                    request.user,
                    form.cleaned_data.get('admin_note', ''),
                )
            except ValidationError as exc:
                self.message_user(request, str(exc.detail), level=messages.ERROR)
                return None
            self.message_user(
                request,
                f'已批量通过 {len(approved)} 笔提现审核，进入待打款',
                level=messages.SUCCESS,
            )
            return None
    else:
        form = BatchApproveForm()
    return _render_batch_action(
        self,
        request,
        queryset,
        form,
        title='批量审核提现申请',
        action_name='approve_selected',
        submit_label='确认审核通过',
    )


@admin.action(description='批量标记已付款（流水号、备注和凭证均选填）')
def mark_paid_selected(self, request, queryset):
    if request.POST.get('confirm_batch_action') == '1':
        form = BatchPayoutForm(request.POST)
        if form.is_valid():
            try:
                batch = mark_withdrawals_paid_batch(
                    queryset.values_list('pk', flat=True),
                    operator=request.user,
                    payment_method=form.cleaned_data['payment_method'],
                    external_transfer_no=form.cleaned_data.get('external_transfer_no', ''),
                    admin_note=form.cleaned_data.get('admin_note', ''),
                    proof_url=form.cleaned_data.get('proof_url', ''),
                )
            except ValidationError as exc:
                self.message_user(request, str(exc.detail), level=messages.ERROR)
                return None
            self.message_user(
                request,
                f'已完成 {batch.item_count} 笔付款，系统批次号：{batch.batch_no}，合计 {batch.total_amount} 鱼干',
                level=messages.SUCCESS,
            )
            return None
    else:
        methods = set(queryset.values_list('payment_method', flat=True))
        initial_method = methods.pop() if len(methods) == 1 else Withdrawal.METHOD_WECHAT
        form = BatchPayoutForm(initial={'payment_method': initial_method})
    return _render_batch_action(
        self,
        request,
        queryset,
        form,
        title='批量确认财务付款',
        action_name='mark_paid_selected',
        submit_label='确认已付款',
    )


@admin.display(description='付款批次')
def payout_batch_no(self, obj):
    item = getattr(obj, 'payout_batch_item', None)
    if not item:
        return '-'
    return item.batch.batch_no


class WithdrawalBatchItemInline(admin.StackedInline):
    model = WithdrawalPayoutBatchItem
    fk_name = 'withdrawal'
    extra = 0
    max_num = 1
    can_delete = False
    fields = ['batch', 'amount_snapshot', 'created_at']
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


withdrawal_admin = admin.site._registry.get(Withdrawal)
if withdrawal_admin:
    admin_class = withdrawal_admin.__class__
    admin_class.approve_selected = approve_selected
    admin_class.mark_paid_selected = mark_paid_selected
    admin_class.payout_batch_no = payout_batch_no

    list_display = list(admin_class.list_display)
    if 'payout_batch_no' not in list_display:
        transfer_index = list_display.index('transfer_no') + 1 if 'transfer_no' in list_display else len(list_display)
        list_display.insert(transfer_index, 'payout_batch_no')
        admin_class.list_display = list_display

    search_fields = list(admin_class.search_fields)
    for field in ['payout_batch_item__batch__batch_no', 'payout_batch_item__batch__external_transfer_no']:
        if field not in search_fields:
            search_fields.append(field)
    admin_class.search_fields = search_fields

    inlines = list(admin_class.inlines)
    if WithdrawalBatchItemInline not in inlines:
        inlines.append(WithdrawalBatchItemInline)
    admin_class.inlines = inlines
