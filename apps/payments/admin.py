from decimal import Decimal

from django.contrib import admin, messages
from django.db import transaction
from django.db.models import Q, Sum
from rest_framework.exceptions import ValidationError

from apps.earnings.services import reverse_refund_earnings

from . import services as payment_services
from .models import Payment, PaymentCallbackLog, Refund, VirtualProductBinding
from .refund_integrity import settle_balance_refund
from .virtualpay import VIRTUAL_CHANNEL, VirtualPaymentAPIError
from .wechat_virtual_refunds import (
    cash_refund_status,
    refund_payment_to_wechat_original,
    sync_payment_wechat_original_refunds,
    sync_wechat_original_refund,
)


class BossUserIdSearchMixin:
    """Allow one admin search box to safely support both text and numeric user IDs.

    Django applies every ``search_fields`` lookup to the same search term. Putting
    an integer ``id__exact`` lookup directly in ``search_fields`` therefore makes
    a text query such as ``chen`` raise ``ValueError: Field 'id' expected a
    number``. Keep ``search_fields`` text-only, then OR in exact numeric ID
    matches only when the entire term is an integer.
    """

    def get_search_results(self, request, queryset, search_term):
        results, may_have_duplicates = super().get_search_results(
            request,
            queryset,
            search_term,
        )
        normalized = str(search_term or '').strip()
        if not normalized.isdigit():
            return results, may_have_duplicates

        user_id = int(normalized)
        id_matches = queryset.filter(
            Q(order__boss_user_id=user_id)
            | Q(order__boss_user__client_profile__id=user_id)
        )
        return results | id_matches, may_have_duplicates


@admin.register(Payment)
class PaymentAdmin(BossUserIdSearchMixin, admin.ModelAdmin):
    list_display = [
        'id', 'payment_no', 'boss_user_id', 'order', 'channel', 'scene',
        'amount', 'status', 'wechat_original_refund_state', 'created_at',
    ]
    list_filter = ['channel', 'scene', 'status']
    search_fields = [
        'payment_no', 'order__order_no', 'third_trade_no',
        'order__boss_user__client_profile__nickname',
        'order__boss_user__client_profile__openid',
    ]
    search_help_text = '可搜索：用户ID、客户资料ID、昵称、OpenID、订单号、支付单号、微信交易号'
    list_select_related = ['order', 'order__boss_user']
    actions = [
        'create_full_refunds',
        'refund_wechat_virtual_original',
        'sync_wechat_virtual_original_refunds',
    ]

    @admin.display(description='用户ID')
    def boss_user_id(self, obj):
        return obj.order.boss_user_id or '-'

    @admin.display(description='微信原路退款')
    def wechat_original_refund_state(self, obj):
        statuses = []
        for refund in obj.refunds.all():
            state = cash_refund_status(refund)
            if state:
                statuses.append(state)
        return ' / '.join(statuses) or '-'

    @admin.action(description='为选中的已支付记录创建剩余金额退款到老板钱包')
    def create_full_refunds(self, request, queryset):
        wallet_succeeded = 0
        pending = 0
        for payment in queryset.select_related('order'):
            if payment.status != 'paid':
                self.message_user(request, f'{payment.payment_no} 不是已支付状态，已跳过', level=messages.WARNING)
                continue
            active_total = payment.refunds.filter(
                status__in=[Refund.STATUS_PENDING, Refund.STATUS_PROCESSING, Refund.STATUS_SUCCEEDED]
            ).aggregate(total=Sum('amount'))['total'] or 0
            remaining = Decimal(str(payment.amount)) - Decimal(str(active_total))
            if remaining <= 0:
                self.message_user(request, f'{payment.payment_no} 已无可退金额', level=messages.WARNING)
                continue
            try:
                refund = payment_services.create_refund(
                    payment.payment_no,
                    remaining,
                    reason='管理员后台发起退款',
                    operator=request.user,
                )
                if refund.status == Refund.STATUS_SUCCEEDED:
                    wallet_succeeded += 1
                else:
                    pending += 1
            except ValidationError as exc:
                self.message_user(request, f'{payment.payment_no}: {exc.detail}', level=messages.WARNING)
        if wallet_succeeded:
            self.message_user(
                request,
                f'{wallet_succeeded} 笔退款已退回老板钱包（前端显示为钻石）',
                level=messages.SUCCESS,
            )
        if pending:
            self.message_user(request, f'{pending} 笔退款仍待处理', level=messages.WARNING)

    @admin.action(description='微信虚拟支付：整单原路退款（统一钻石模型）')
    def refund_wechat_virtual_original(self, request, queryset):
        success_count = 0
        for payment in queryset.select_related('order', 'order__boss_user'):
            if payment.channel != VIRTUAL_CHANNEL:
                self.message_user(
                    request,
                    f'{payment.payment_no} 不是微信虚拟支付，已跳过',
                    level=messages.WARNING,
                )
                continue
            try:
                refund = refund_payment_to_wechat_original(
                    payment,
                    operator=request.user,
                    reason='管理员后台微信原路退款',
                )
            except (ValidationError, VirtualPaymentAPIError) as exc:
                detail = getattr(exc, 'detail', str(exc))
                self.message_user(request, f'{payment.payment_no}: {detail}', level=messages.WARNING)
                continue
            success_count += 1
            self.message_user(
                request,
                f'{payment.payment_no} 已提交微信原路退款，退款单 {refund.refund_no}，请稍后同步状态',
                level=messages.SUCCESS,
            )
        if success_count:
            self.message_user(
                request,
                (
                    f'共提交 {success_count} 笔微信原路退款。未发生过钻石退款的订单不会变动钱包；'
                    '已退过钻石的订单会自动扣回对应钻石后再转微信原路退款。'
                ),
                level=messages.SUCCESS,
            )

    @admin.action(description='微信虚拟支付：同步原路退款状态')
    def sync_wechat_virtual_original_refunds(self, request, queryset):
        synced_count = 0
        for payment in queryset.select_related('order', 'order__boss_user'):
            try:
                refunds = sync_payment_wechat_original_refunds(payment, operator=request.user)
            except (ValidationError, VirtualPaymentAPIError) as exc:
                detail = getattr(exc, 'detail', str(exc))
                self.message_user(request, f'{payment.payment_no}: {detail}', level=messages.WARNING)
                continue
            synced_count += len(refunds)
            states = ', '.join(f'{refund.refund_no}:{cash_refund_status(refund)}' for refund in refunds)
            self.message_user(request, f'{payment.payment_no} → {states}', level=messages.INFO)
        if synced_count:
            self.message_user(request, f'已同步 {synced_count} 笔微信原路退款状态', level=messages.SUCCESS)


@admin.register(Refund)
class RefundAdmin(BossUserIdSearchMixin, admin.ModelAdmin):
    list_display = [
        'refund_no', 'payment', 'order', 'amount', 'status',
        'wechat_original_refund_state', 'third_refund_no', 'created_by', 'created_at',
    ]
    list_filter = ['status', 'payment__channel', 'created_at']
    search_fields = [
        'refund_no', 'payment__payment_no', 'order__order_no', 'third_refund_no', 'reason',
        'order__boss_user__client_profile__nickname',
        'order__boss_user__client_profile__openid',
    ]
    search_help_text = '可搜索：用户ID、客户资料ID、昵称、OpenID、订单号、支付单号、退款单号'
    readonly_fields = [
        'refund_no', 'payment', 'order', 'amount', 'reason', 'status',
        'third_refund_no', 'notify_payload', 'created_by', 'created_at', 'updated_at',
    ]
    fields = [
        'refund_no', 'payment', 'order', 'amount', 'reason', 'status',
        'third_refund_no', 'failed_reason', 'notify_payload',
        'created_by', 'created_at', 'updated_at',
    ]
    actions = ['confirm_refund_succeeded', 'mark_refund_failed', 'sync_selected_wechat_original_refunds']

    @admin.display(description='微信原路退款')
    def wechat_original_refund_state(self, obj):
        return cash_refund_status(obj) or '-'

    @admin.action(description='同步选中记录的微信原路退款状态')
    def sync_selected_wechat_original_refunds(self, request, queryset):
        synced_count = 0
        for refund in queryset.select_related('payment', 'order', 'order__boss_user'):
            if not cash_refund_status(refund):
                continue
            try:
                synced = sync_wechat_original_refund(refund, operator=request.user)
            except (ValidationError, VirtualPaymentAPIError) as exc:
                detail = getattr(exc, 'detail', str(exc))
                self.message_user(request, f'{refund.refund_no}: {detail}', level=messages.WARNING)
                continue
            synced_count += 1
            self.message_user(
                request,
                f'{refund.refund_no}: {cash_refund_status(synced)}',
                level=messages.INFO,
            )
        if synced_count:
            self.message_user(request, f'已同步 {synced_count} 笔退款', level=messages.SUCCESS)

    @admin.action(description='确认退款成功并自动冲销陪玩工资')
    def confirm_refund_succeeded(self, request, queryset):
        success_count = 0
        for refund in queryset:
            try:
                cash_state = cash_refund_status(refund)
                if cash_state:
                    if cash_state == 'succeeded':
                        success_count += 1
                        continue
                    raise ValidationError({
                        'detail': '微信原路退款不能人工确认成功，必须通过“同步微信原路退款状态”由微信结果确认'
                    })

                if refund.payment.channel == 'balance':
                    settled = settle_balance_refund(refund.pk, operator=request.user)
                    if settled.status == Refund.STATUS_SUCCEEDED:
                        success_count += 1
                    continue

                with transaction.atomic():
                    locked = Refund.objects.select_for_update().select_related('payment', 'order').get(pk=refund.pk)
                    if locked.status == Refund.STATUS_SUCCEEDED:
                        reverse_refund_earnings(locked, operator=request.user)
                        continue
                    if locked.status not in {Refund.STATUS_PENDING, Refund.STATUS_PROCESSING}:
                        raise ValidationError({'detail': '只有待处理或退款中的记录可以确认成功'})
                    locked.status = Refund.STATUS_SUCCEEDED
                    locked.failed_reason = ''
                    locked.save(update_fields=['status', 'failed_reason', 'updated_at'])
                    reverse_refund_earnings(locked, operator=request.user)

                    succeeded_total = locked.payment.refunds.filter(
                        status=Refund.STATUS_SUCCEEDED
                    ).aggregate(total=Sum('amount'))['total'] or 0
                    if Decimal(str(succeeded_total)) >= Decimal(str(locked.payment.amount)):
                        locked.payment.status = 'refunded'
                        locked.payment.save(update_fields=['status', 'updated_at'])
                    success_count += 1
            except ValidationError as exc:
                self.message_user(request, f'{refund.refund_no}: {exc.detail}', level=messages.WARNING)
        if success_count:
            self.message_user(request, f'已确认 {success_count} 笔退款成功，并完成相关账务处理', level=messages.SUCCESS)

    @admin.action(description='标记退款失败')
    def mark_refund_failed(self, request, queryset):
        updated = 0
        for refund in queryset.select_related('payment'):
            if cash_refund_status(refund):
                self.message_user(
                    request,
                    f'{refund.refund_no}: 微信原路退款不能人工标记失败，请同步微信状态',
                    level=messages.WARNING,
                )
                continue
            if refund.payment.channel == 'balance':
                continue
            if refund.status in {Refund.STATUS_PENDING, Refund.STATUS_PROCESSING}:
                Refund.objects.filter(pk=refund.pk).update(
                    status=Refund.STATUS_FAILED,
                    failed_reason='管理员标记退款失败',
                )
                updated += 1
        self.message_user(
            request,
            f'已标记 {updated} 笔第三方退款失败；余额退款和微信原路退款不能手动标记失败。',
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(VirtualProductBinding)
class VirtualProductBindingAdmin(admin.ModelAdmin):
    list_display = ['id', 'product_id', 'binding_target', 'goods_price_yuan', 'is_active', 'updated_at']
    list_filter = ['is_active']
    search_fields = ['product_id', 'package__name', 'spec__name', 'spec__package__name', 'remark']
    autocomplete_fields = ['package', 'spec']
    list_editable = ['is_active']

    def get_urls(self):
        from django.urls import path
        urls = super().get_urls()
        custom = [
            path('sync-bindings/', self.sync_bindings_view, name='sync_virtual_bindings'),
        ]
        return custom + urls

    def sync_bindings_view(self, request):
        """一键同步：为所有未绑定商品规格按命名规则创建 VirtualProductBinding。"""
        from django.contrib import messages
        from django.shortcuts import redirect
        from django.urls import reverse
        from apps.payments.virtual_item_admin import sync_bindings_from_wechat_items

        try:
            stats = sync_bindings_from_wechat_items()
            messages.success(
                request,
                f'同步完成：新建 {stats["created"]} 条，'
                f'跳过重复 {stats["skipped_duplicate"]} 条，'
                f'跳过无效 {stats["skipped_invalid"]} 条。'
                f'新建绑定对应的微信道具需先在微信后台导入，否则支付会报错。',
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception('一键同步虚拟道具绑定失败')
            messages.error(request, f'同步失败：{exc}')
        return redirect(reverse('admin:payments_virtualproductbinding_changelist'))

    @admin.display(description='绑定对象')
    def binding_target(self, obj):
        return obj.spec or obj.package

    @admin.display(description='单价')
    def goods_price_yuan(self, obj):
        return f'¥{obj.goods_price_fen / 100:.2f}'

    @admin.action(description='一键同步未绑定商品的道具绑定（全部商品）')
    def sync_bindings_from_wechat(self, request, queryset):
        """为所有未绑定商品规格按命名规则自动创建 VirtualProductBinding。"""
        try:
            from apps.payments.virtual_item_admin import sync_bindings_from_wechat_items
            stats = sync_bindings_from_wechat_items()
            self.message_user(
                request,
                f'同步完成：新建 {stats["created"]} 条，'
                f'跳过重复 {stats["skipped_duplicate"]} 条，'
                f'跳过无效 {stats["skipped_invalid"]} 条。'
                f'注意：新建的绑定对应的微信道具需先在微信后台导入，否则支付会报错。',
            )
        except Exception as exc:  # pragma: no cover - admin helper
            import logging
            logging.getLogger(__name__).exception('一键同步虚拟道具绑定失败')
            self.message_user(request, f'同步失败：{exc}', level='error')


@admin.register(PaymentCallbackLog)
class PaymentCallbackLogAdmin(admin.ModelAdmin):
    list_display = ['id', 'payment_no', 'channel', 'verify_result', 'handled_result', 'created_at']
    list_filter = ['channel', 'verify_result']
