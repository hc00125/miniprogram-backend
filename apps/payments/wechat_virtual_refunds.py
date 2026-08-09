import logging
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.wallet.services import get_or_lock_wallet, qmoney, write_wallet_ledger

from .models import Payment, Refund
from .refund_integrity import ensure_payment_refund
from .services import amount_to_cents
from .virtualpay import (
    VIRTUAL_CHANNEL,
    VirtualPaymentAPIError,
    compact_json,
    virtual_env,
    xpay_post,
)


logger = logging.getLogger(__name__)

CASH_REFUND_META_KEY = 'wechat_original_refund'
CASH_STATUS_PREPARING = 'preparing'
CASH_STATUS_PROCESSING = 'processing'
CASH_STATUS_UNKNOWN = 'unknown'
CASH_STATUS_SUCCEEDED = 'succeeded'
CASH_STATUS_FAILED = 'failed'

# 微信虚拟支付 query_order 的订单状态：5=已退款，6=已关闭，7=退款失败。
XPAY_STATUS_REFUNDED = 5
XPAY_STATUS_CLOSED = 6
XPAY_STATUS_REFUND_FAILED = 7
XPAY_ORDER_TYPE_REFUND = 1

# 钱包底层仍以“人民币等值余额”记账，前端固定按 1 元 = 10 钻石展示。
# 这里用后台调整流水实现“扣回/恢复钻石”，不改变现有钱包表结构。
WALLET_ENTRY_TYPE = 'admin_adjust'


def _qmoney(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _cash_meta(refund):
    payload = refund.notify_payload if isinstance(refund.notify_payload, dict) else {}
    meta = payload.get(CASH_REFUND_META_KEY) or {}
    return dict(meta) if isinstance(meta, dict) else {}


def cash_refund_status(refund):
    return _cash_meta(refund).get('status') or ''


def _save_cash_meta(refund_id, meta, *, third_refund_no=None):
    with transaction.atomic():
        refund = Refund.objects.select_for_update().get(pk=refund_id)
        payload = refund.notify_payload if isinstance(refund.notify_payload, dict) else {}
        payload = {**payload, CASH_REFUND_META_KEY: meta}
        refund.notify_payload = payload
        update_fields = ['notify_payload', 'updated_at']
        if third_refund_no is not None:
            refund.third_refund_no = third_refund_no or refund.third_refund_no
            update_fields.append('third_refund_no')
        refund.save(update_fields=update_fields)
        return refund


def _profile_for_refund(refund):
    order = refund.order
    boss_user = order.boss_user if order and order.boss_user_id else None
    profile = getattr(boss_user, 'client_profile', None) if boss_user else None
    if not profile:
        raise ValidationError({'detail': '找不到该订单对应的老板钱包账户'})
    if not profile.openid:
        raise ValidationError({'detail': '该老板账号缺少微信 openid，不能发起微信原路退款'})
    return profile


def _query_xpay_order(*, openid, order_id):
    response = xpay_post('/xpay/query_order', {
        'openid': openid,
        'env': virtual_env(),
        'order_id': order_id,
    })
    return response, dict(response.get('order') or {})


def _restore_wallet_deduction(refund, meta, *, operator=None, note='微信原路退款失败，恢复钻石'):
    debit_reference = meta.get('wallet_debit_reference') or ''
    if not debit_reference:
        return False

    profile = _profile_for_refund(refund)
    restore_reference = f'{debit_reference}:restore'
    amount = qmoney(refund.amount)
    with transaction.atomic():
        wallet = get_or_lock_wallet(profile)
        _entry, created = write_wallet_ledger(
            wallet,
            WALLET_ENTRY_TYPE,
            amount,
            reference_type='wechat_virtual_refund_restore',
            reference_id=restore_reference,
            note=note,
            operator=operator,
        )
    return created


def convert_refund_to_wechat_original(refund_or_id, *, operator=None):
    """把一笔已经退到平台钱包的退款转换为微信虚拟支付原路退款。

    业务退款本身仍由 Refund 表记录并保持 succeeded；本函数只负责：
    1. 从老板钱包扣回相同人民币等值钻石；
    2. 调用 /xpay/refund_order 发起真实微信退款；
    3. 把微信原路退款状态写入 notify_payload.wechat_original_refund。

    这样普通前端退款逻辑完全不变，同时不会出现“钱包和微信各退一次”。
    """
    refund_id = getattr(refund_or_id, 'pk', refund_or_id)
    refund = (
        Refund.objects
        .select_related('payment', 'order', 'order__boss_user')
        .get(pk=refund_id)
    )
    payment = refund.payment
    if payment.channel != VIRTUAL_CHANNEL:
        raise ValidationError({'detail': '只有微信虚拟支付订单可以微信原路退款'})
    if refund.status != Refund.STATUS_SUCCEEDED:
        raise ValidationError({'detail': '请先完成平台钱包退款，再转换为微信原路退款'})

    existing_meta = _cash_meta(refund)
    existing_status = existing_meta.get('status')
    if existing_status == CASH_STATUS_SUCCEEDED:
        return refund
    if existing_status in {CASH_STATUS_PREPARING, CASH_STATUS_PROCESSING, CASH_STATUS_UNKNOWN}:
        raise ValidationError({'detail': '该退款已提交微信，请先同步微信退款状态，不要重复提交'})

    profile = _profile_for_refund(refund)
    refund_fee = amount_to_cents(refund.amount)
    payment_fee = amount_to_cents(payment.amount)

    # 微信要求 left_fee 使用支付单当前剩余可退金额，因此先实时查原支付单。
    _query_response, payment_order = _query_xpay_order(
        openid=profile.openid,
        order_id=payment.payment_no,
    )
    left_fee = int(payment_order.get('left_fee') or 0)
    if left_fee <= 0:
        raise ValidationError({'detail': '微信侧该支付单已无可退金额，请先同步/人工核对'})
    if refund_fee > left_fee:
        raise ValidationError({
            'detail': f'退款金额{refund_fee}分超过微信侧剩余可退金额{left_fee}分，请人工核对'
        })
    if payment_fee <= 0:
        raise ValidationError({'detail': '原支付金额不正确'})

    # 每次明确失败后允许再次尝试；每次尝试使用独立的钱包流水引用，保证扣款/恢复均幂等。
    attempt = int(existing_meta.get('attempts') or 0) + 1
    debit_reference = f'{refund.refund_no}:cash:{attempt}'
    now = timezone.now()

    with transaction.atomic():
        locked_refund = (
            Refund.objects
            .select_for_update(of=('self',))
            .select_related('payment', 'order', 'order__boss_user')
            .get(pk=refund.pk)
        )
        locked_meta = _cash_meta(locked_refund)
        locked_status = locked_meta.get('status')
        if locked_status == CASH_STATUS_SUCCEEDED:
            return locked_refund
        if locked_status in {CASH_STATUS_PREPARING, CASH_STATUS_PROCESSING, CASH_STATUS_UNKNOWN}:
            raise ValidationError({'detail': '该退款已提交微信，请先同步微信退款状态'})

        wallet = get_or_lock_wallet(profile)
        amount = qmoney(locked_refund.amount)
        if qmoney(wallet.balance) < amount:
            raise ValidationError({
                'detail': (
                    f'老板钱包当前仅剩💎{int(qmoney(wallet.balance) * 10)}，'
                    f'需要扣回💎{int(amount * 10)}后才能微信原路退款。'
                )
            })

        _entry, _created = write_wallet_ledger(
            wallet,
            WALLET_ENTRY_TYPE,
            -amount,
            reference_type='wechat_virtual_refund',
            reference_id=debit_reference,
            note=f'微信原路退款 {locked_refund.refund_no}：扣回对应钻石',
            operator=operator,
        )

        meta = {
            **locked_meta,
            'status': CASH_STATUS_PREPARING,
            'attempts': attempt,
            'refund_order_id': locked_refund.refund_no,
            'refund_fee': refund_fee,
            'left_fee_at_submit': left_fee,
            'wallet_debit_reference': debit_reference,
            'wallet_deducted_yuan': str(amount),
            'wallet_deducted_diamonds': int(amount * 10),
            'prepared_at': now.isoformat(),
        }
        payload = locked_refund.notify_payload if isinstance(locked_refund.notify_payload, dict) else {}
        locked_refund.notify_payload = {**payload, CASH_REFUND_META_KEY: meta}
        locked_refund.save(update_fields=['notify_payload', 'updated_at'])

    request_payload = {
        'openid': profile.openid,
        'order_id': payment.payment_no,
        'refund_order_id': refund.refund_no,
        'left_fee': left_fee,
        'refund_fee': refund_fee,
        'biz_meta': compact_json({
            'order_no': payment.order_id,
            'payment_no': payment.payment_no,
            'refund_no': refund.refund_no,
        }),
        'refund_reason': '3',
        'req_from': '1',
        'env': virtual_env(),
    }

    try:
        response = xpay_post('/xpay/refund_order', request_payload)
    except VirtualPaymentAPIError as exc:
        # 网络错误存在“微信已收到但本地没收到响应”的不确定性，不能贸然把钻石补回，
        # 否则可能再次形成双退。保留扣款并要求后台同步退款单状态。
        uncertain = str(getattr(exc, 'code', '')) == 'network_error'
        meta = {
            **meta,
            'status': CASH_STATUS_UNKNOWN if uncertain else CASH_STATUS_FAILED,
            'submit_error': {
                'code': getattr(exc, 'code', ''),
                'message': str(exc)[:500],
            },
            'updated_at': timezone.now().isoformat(),
        }
        if not uncertain:
            refund = Refund.objects.select_related('payment', 'order', 'order__boss_user').get(pk=refund.pk)
            _restore_wallet_deduction(
                refund,
                meta,
                operator=operator,
                note=f'微信原路退款 {refund.refund_no} 提交失败，恢复对应钻石',
            )
            meta['wallet_restored'] = True
            meta['wallet_restored_at'] = timezone.now().isoformat()
        _save_cash_meta(refund.pk, meta)
        raise

    meta = {
        **meta,
        'status': CASH_STATUS_PROCESSING,
        'submitted_at': timezone.now().isoformat(),
        'submit_response': response,
    }
    third_refund_no = response.get('refund_wx_order_id') or response.get('refund_order_id') or ''
    return _save_cash_meta(refund.pk, meta, third_refund_no=third_refund_no)


def sync_wechat_original_refund(refund_or_id, *, operator=None):
    """通过 xpay/query_order 同步真实微信退款单状态。"""
    refund_id = getattr(refund_or_id, 'pk', refund_or_id)
    refund = (
        Refund.objects
        .select_related('payment', 'order', 'order__boss_user')
        .get(pk=refund_id)
    )
    meta = _cash_meta(refund)
    if not meta:
        raise ValidationError({'detail': '该退款尚未发起微信原路退款'})
    if meta.get('status') == CASH_STATUS_SUCCEEDED:
        return refund

    profile = _profile_for_refund(refund)
    _response, order_data = _query_xpay_order(
        openid=profile.openid,
        order_id=meta.get('refund_order_id') or refund.refund_no,
    )
    order_type = int(order_data.get('order_type') or 0)
    remote_status = int(order_data.get('status') or 0)
    remote_refund_fee = int(order_data.get('refund_fee') or 0)
    expected_refund_fee = amount_to_cents(refund.amount)

    if order_type != XPAY_ORDER_TYPE_REFUND:
        raise ValidationError({'detail': '微信返回的不是退款单，请人工核对退款单号'})
    if remote_refund_fee and remote_refund_fee != expected_refund_fee:
        raise ValidationError({'detail': '微信退款金额与本地退款金额不一致，请人工核对'})

    meta = {
        **meta,
        'query_order': order_data,
        'remote_status': remote_status,
        'last_synced_at': timezone.now().isoformat(),
    }

    if remote_status == XPAY_STATUS_REFUNDED:
        meta['status'] = CASH_STATUS_SUCCEEDED
        meta['succeeded_at'] = timezone.now().isoformat()
        third_refund_no = order_data.get('wx_order_id') or refund.third_refund_no or ''
        return _save_cash_meta(refund.pk, meta, third_refund_no=third_refund_no)

    if remote_status in {XPAY_STATUS_CLOSED, XPAY_STATUS_REFUND_FAILED}:
        meta['status'] = CASH_STATUS_FAILED
        refund = Refund.objects.select_related('payment', 'order', 'order__boss_user').get(pk=refund.pk)
        restored = _restore_wallet_deduction(
            refund,
            meta,
            operator=operator,
            note=f'微信原路退款 {refund.refund_no} 终态失败，恢复对应钻石',
        )
        if restored:
            meta['wallet_restored'] = True
            meta['wallet_restored_at'] = timezone.now().isoformat()
        return _save_cash_meta(refund.pk, meta)

    meta['status'] = CASH_STATUS_PROCESSING
    return _save_cash_meta(refund.pk, meta)


def refund_payment_to_wechat_original(payment_or_id, *, operator=None, reason='管理员微信原路退款'):
    """后台整单微信原路退款入口。

    先复用现有退款体系确保“整单金额已经退成钻石”，再把这笔钻石退款转换成微信现金退款。
    对用户而言钱包净变化为0；如果之前已经拿到并保留了钻石退款，则会直接扣回对应钻石。
    """
    payment_id = getattr(payment_or_id, 'pk', payment_or_id)
    payment = (
        Payment.objects
        .select_related('order', 'order__boss_user')
        .get(pk=payment_id)
    )
    if payment.channel != VIRTUAL_CHANNEL:
        raise ValidationError({'detail': '只有微信虚拟支付记录可以执行微信原路退款'})
    if payment.status not in {'paid', 'refunded'}:
        raise ValidationError({'detail': '该支付记录当前状态不能退款'})

    ensure_payment_refund(
        payment.payment_no,
        _qmoney(payment.amount),
        reason=reason,
        operator=operator,
    )

    succeeded = list(
        Refund.objects
        .filter(payment=payment, status=Refund.STATUS_SUCCEEDED)
        .order_by('created_at', 'id')
    )
    total = sum((_qmoney(item.amount) for item in succeeded), Decimal('0.00'))
    if total != _qmoney(payment.amount):
        raise ValidationError({'detail': '钱包退款金额未达到整单金额，暂不能执行微信整单原路退款'})
    if len(succeeded) != 1:
        raise ValidationError({
            'detail': '该支付单存在多笔部分退款；第一版微信原路退款仅支持单笔整单退款，请人工处理'
        })

    return convert_refund_to_wechat_original(succeeded[0], operator=operator)


def sync_payment_wechat_original_refunds(payment_or_id, *, operator=None):
    payment_id = getattr(payment_or_id, 'pk', payment_or_id)
    refunds = list(Refund.objects.filter(payment_id=payment_id).order_by('created_at', 'id'))
    synced = []
    for refund in refunds:
        if _cash_meta(refund):
            synced.append(sync_wechat_original_refund(refund, operator=operator))
    if not synced:
        raise ValidationError({'detail': '该支付单没有已提交的微信原路退款'})
    return synced
