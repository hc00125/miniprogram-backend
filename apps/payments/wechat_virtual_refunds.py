import logging
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.wallet.models import ClientWalletLedger
from apps.wallet.services import get_or_lock_wallet, qmoney, write_wallet_ledger

from .diamond_settlement_signals import settle_virtual_payment_diamonds
from .models import Payment, Refund
from .services import amount_to_cents, generate_refund_no
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

CASH_MODE_DIRECT = 'direct_wechat_refund'
CASH_MODE_CONVERTED = 'converted_from_wallet_refund'

# 微信虚拟支付 query_order 的退款单状态。
XPAY_STATUS_REFUNDED = 5
XPAY_STATUS_CLOSED = 6
XPAY_STATUS_REFUND_FAILED = 7
XPAY_ORDER_TYPE_REFUND = 1

ACTIVE_REFUND_STATUSES = (
    Refund.STATUS_PENDING,
    Refund.STATUS_PROCESSING,
    Refund.STATUS_SUCCEEDED,
)


def _qmoney(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _operator_or_none(operator):
    return operator if getattr(operator, 'is_authenticated', False) else None


def _cash_meta(refund):
    payload = refund.notify_payload if isinstance(refund.notify_payload, dict) else {}
    meta = payload.get(CASH_REFUND_META_KEY) or {}
    return dict(meta) if isinstance(meta, dict) else {}


def cash_refund_status(refund):
    return _cash_meta(refund).get('status') or ''


def _profile_for_payment(payment):
    order = payment.order
    boss_user = order.boss_user if order and order.boss_user_id else None
    profile = getattr(boss_user, 'client_profile', None) if boss_user else None
    if not profile:
        raise ValidationError({'detail': '找不到该支付记录对应的老板钱包账户'})
    if not profile.openid:
        raise ValidationError({'detail': '该老板账号缺少微信 openid，不能发起微信原路退款'})
    return profile


def _profile_for_refund(refund):
    return _profile_for_payment(refund.payment)


def _query_xpay_order(*, openid, order_id):
    response = xpay_post('/xpay/query_order', {
        'openid': openid,
        'env': virtual_env(),
        'order_id': order_id,
    })
    return response, dict(response.get('order') or {})


def _query_xpay_refund_order(*, openid, refund_order_id):
    """通过 xpay/query_refund_order 查询退款单状态（端点与 query_order 不同，pay_sig 算法一致）。"""
    response = xpay_post('/xpay/query_refund_order', {
        'openid': openid,
        'env': virtual_env(),
        'refund_order_id': refund_order_id,
    })
    return response, dict(response.get('order') or {})


def _preflight_wechat_refund(payment, profile, refund_fee):
    _response, order_data = _query_xpay_order(
        openid=profile.openid,
        order_id=payment.payment_no,
    )
    left_fee = int(order_data.get('left_fee') or 0)
    if left_fee <= 0:
        raise ValidationError({'detail': '微信侧该支付单已无可退金额，请先同步或人工核对'})
    if refund_fee > left_fee:
        raise ValidationError({
            'detail': f'退款金额{refund_fee}分超过微信侧剩余可退金额{left_fee}分，请人工核对'
        })
    return left_fee


def _save_cash_meta(refund_id, meta, *, third_refund_no=None, refund_status=None, failed_reason=None):
    with transaction.atomic():
        refund = Refund.objects.select_for_update().get(pk=refund_id)
        payload = refund.notify_payload if isinstance(refund.notify_payload, dict) else {}
        refund.notify_payload = {**payload, CASH_REFUND_META_KEY: meta}
        update_fields = ['notify_payload', 'updated_at']
        if third_refund_no is not None:
            refund.third_refund_no = third_refund_no or refund.third_refund_no
            update_fields.append('third_refund_no')
        if refund_status is not None:
            refund.status = refund_status
            update_fields.append('status')
        if failed_reason is not None:
            refund.failed_reason = failed_reason
            update_fields.append('failed_reason')
        refund.save(update_fields=list(dict.fromkeys(update_fields)))
        return refund


def _wallet_conversion_reference(refund, attempt=1):
    base = f'{refund.refund_no}:wechat-original'
    return base if attempt <= 1 else f'{base}:{attempt}'


def _deduct_wallet_refund_for_conversion(refund, reference, *, operator=None):
    """仅用于“已经退过钻石，后来改成微信原路退款”的兼容场景。"""
    profile = _profile_for_refund(refund)
    amount = qmoney(refund.amount)

    with transaction.atomic():
        wallet = get_or_lock_wallet(profile)
        existing = ClientWalletLedger.objects.filter(
            wallet=wallet,
            entry_type=ClientWalletLedger.TYPE_WECHAT_REFUND_CONVERSION,
            reference_id=reference,
        ).first()
        if existing:
            return existing, False
        if qmoney(wallet.balance) < amount:
            raise ValidationError({
                'detail': (
                    f'老板钱包当前仅剩💎{int(qmoney(wallet.balance) * 10)}，'
                    f'需要扣回💎{int(amount * 10)}后才能把钻石退款改成微信原路退款。'
                )
            })
        return write_wallet_ledger(
            wallet,
            ClientWalletLedger.TYPE_WECHAT_REFUND_CONVERSION,
            -amount,
            reference_type='refund',
            reference_id=reference,
            note=f'退款 {refund.refund_no} 从钻石退款转换为微信原路退款',
            operator=operator,
        )


def _restore_wallet_conversion(refund, meta, *, operator=None):
    profile = _profile_for_refund(refund)
    amount = qmoney(refund.amount)
    reference = meta.get('wallet_conversion_reference') or ''
    if not reference:
        return False
    restore_reference = f'{reference}:restore'

    with transaction.atomic():
        wallet = get_or_lock_wallet(profile)
        debit = ClientWalletLedger.objects.filter(
            wallet=wallet,
            entry_type=ClientWalletLedger.TYPE_WECHAT_REFUND_CONVERSION,
            reference_id=reference,
        ).first()
        if not debit:
            return False
        _entry, created = write_wallet_ledger(
            wallet,
            ClientWalletLedger.TYPE_WECHAT_REFUND_RESTORE,
            amount,
            reference_type='refund',
            reference_id=restore_reference,
            note=f'微信原路退款 {refund.refund_no} 失败，恢复对应钻石',
            operator=operator,
        )
        return created


def _build_request_payload(refund, payment, profile, left_fee):
    return {
        'openid': profile.openid,
        'order_id': payment.payment_no,
        'refund_order_id': refund.refund_no,
        'left_fee': left_fee,
        'refund_fee': amount_to_cents(refund.amount),
        'biz_meta': compact_json({
            'order_no': payment.order_id,
            'payment_no': payment.payment_no,
            'refund_no': refund.refund_no,
        }),
        'refund_reason': '3',
        'req_from': '1',
        'env': virtual_env(),
    }


def _mark_submit_failure(refund, meta, exc, *, operator=None):
    uncertain = str(getattr(exc, 'code', '')) == 'network_error'
    mode = meta.get('mode')
    meta = {
        **meta,
        'status': CASH_STATUS_UNKNOWN if uncertain else CASH_STATUS_FAILED,
        'submit_error': {
            'code': getattr(exc, 'code', ''),
            'message': str(exc)[:500],
        },
        'updated_at': timezone.now().isoformat(),
    }

    # 网络错误不能判断微信是否已经收到任务，因此不能先恢复钻石或把退款判失败。
    if uncertain:
        return _save_cash_meta(refund.pk, meta)

    if mode == CASH_MODE_CONVERTED:
        restored = _restore_wallet_conversion(refund, meta, operator=operator)
        if restored:
            meta['wallet_restored'] = True
            meta['wallet_restored_at'] = timezone.now().isoformat()
        # 这笔业务退款本身仍然是“钻石退款成功”，只有现金转换失败。
        return _save_cash_meta(refund.pk, meta)

    # 直接微信原路退款没有给钱包增加钻石，提交明确失败即可把退款单标记失败。
    return _save_cash_meta(
        refund.pk,
        meta,
        refund_status=Refund.STATUS_FAILED,
        failed_reason=str(exc)[:300],
    )


def _submit_wechat_refund(refund, *, operator=None):
    payment = refund.payment
    profile = _profile_for_refund(refund)
    meta = _cash_meta(refund)
    refund_fee = amount_to_cents(refund.amount)
    left_fee = _preflight_wechat_refund(payment, profile, refund_fee)

    meta = {
        **meta,
        'status': CASH_STATUS_PREPARING,
        'refund_order_id': refund.refund_no,
        'refund_fee': refund_fee,
        'left_fee_at_submit': left_fee,
        'prepared_at': timezone.now().isoformat(),
    }
    _save_cash_meta(refund.pk, meta)

    try:
        response = xpay_post(
            '/xpay/refund_order',
            _build_request_payload(refund, payment, profile, left_fee),
        )
    except VirtualPaymentAPIError as exc:
        _mark_submit_failure(refund, meta, exc, operator=operator)
        raise

    meta = {
        **meta,
        'status': CASH_STATUS_PROCESSING,
        'submitted_at': timezone.now().isoformat(),
        'submit_response': response,
    }
    third_refund_no = response.get('refund_wx_order_id') or response.get('refund_order_id') or ''
    return _save_cash_meta(refund.pk, meta, third_refund_no=third_refund_no)


def _create_direct_wechat_refund(payment, *, operator=None, reason='管理员微信原路退款'):
    amount = _qmoney(payment.amount)
    with transaction.atomic():
        payment = (
            Payment.objects
            .select_for_update(of=('self',))
            .select_related('order', 'order__boss_user')
            .get(pk=payment.pk)
        )
        active = list(
            Refund.objects
            .select_for_update(of=('self',))
            .filter(payment=payment, status__in=ACTIVE_REFUND_STATUSES)
            .order_by('created_at', 'id')
        )
        if active:
            raise ValidationError({'detail': '该支付单已经存在退款记录，请先处理已有退款'})

        meta = {
            'status': CASH_STATUS_PREPARING,
            'mode': CASH_MODE_DIRECT,
            'diamond_refund_created': False,
            'created_at': timezone.now().isoformat(),
        }
        refund = Refund.objects.create(
            refund_no=generate_refund_no(),
            payment=payment,
            order=payment.order,
            amount=amount,
            reason=reason or '',
            status=Refund.STATUS_PROCESSING,
            notify_payload={CASH_REFUND_META_KEY: meta},
            created_by=_operator_or_none(operator),
        )
        return refund


def convert_refund_to_wechat_original(refund_or_id, *, operator=None):
    """把已经成功退到钱包的整单钻石退款转换成微信原路退款。"""
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
        raise ValidationError({'detail': '只有已经退回钻石的钱包退款才能转换为微信原路退款'})
    if _qmoney(refund.amount) != _qmoney(payment.amount):
        raise ValidationError({'detail': '第一版仅支持整单钻石退款转换为微信原路退款'})

    existing_meta = _cash_meta(refund)
    existing_status = existing_meta.get('status')
    if existing_status == CASH_STATUS_SUCCEEDED:
        return refund
    if existing_status in {CASH_STATUS_PREPARING, CASH_STATUS_PROCESSING, CASH_STATUS_UNKNOWN}:
        raise ValidationError({'detail': '该退款已经提交微信，请先同步状态'})

    attempt = int(existing_meta.get('attempts') or 0) + 1
    conversion_reference = _wallet_conversion_reference(refund, attempt)
    profile = _profile_for_refund(refund)
    left_fee = _preflight_wechat_refund(payment, profile, amount_to_cents(refund.amount))
    _entry, _created = _deduct_wallet_refund_for_conversion(
        refund,
        conversion_reference,
        operator=operator,
    )

    meta = {
        **existing_meta,
        'status': CASH_STATUS_PREPARING,
        'mode': CASH_MODE_CONVERTED,
        'attempts': attempt,
        'diamond_refund_created': True,
        'wallet_conversion_reference': conversion_reference,
        'left_fee_at_prepare': left_fee,
        'prepared_at': timezone.now().isoformat(),
    }
    meta.pop('wallet_restored', None)
    meta.pop('wallet_restored_at', None)
    refund = _save_cash_meta(refund.pk, meta)
    return _submit_wechat_refund(refund, operator=operator)


def refund_payment_to_wechat_original(payment_or_id, *, operator=None, reason='管理员微信原路退款'):
    """后台整单微信原路退款入口。

    统一钻石模型下：
    - 从未退过钻石：直接创建微信退款，钱包不增不减；
    - 已经退过钻石：只扣回那笔已退钻石，再把它转换成微信原路退款。
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

    # 老订单也在首次操作时补齐“人民币购钻石 → 钻石消费”的隐藏桥接账，净余额不变。
    settle_virtual_payment_diamonds(payment.pk)
    payment.refresh_from_db()

    active = list(
        Refund.objects
        .filter(payment=payment, status__in=ACTIVE_REFUND_STATUSES)
        .order_by('created_at', 'id')
    )

    for item in active:
        state = cash_refund_status(item)
        if state == CASH_STATUS_SUCCEEDED:
            return item
        if state in {CASH_STATUS_PREPARING, CASH_STATUS_PROCESSING, CASH_STATUS_UNKNOWN}:
            raise ValidationError({'detail': '该支付单已有微信原路退款任务，请先同步状态'})

    succeeded = [item for item in active if item.status == Refund.STATUS_SUCCEEDED]
    unfinished = [item for item in active if item.status in {Refund.STATUS_PENDING, Refund.STATUS_PROCESSING}]
    if unfinished:
        raise ValidationError({'detail': '该支付单存在尚未完成的钱包退款，请先处理'})

    if succeeded:
        total = sum((_qmoney(item.amount) for item in succeeded), Decimal('0.00'))
        if total != _qmoney(payment.amount) or len(succeeded) != 1:
            raise ValidationError({'detail': '该支付单存在部分/多笔钻石退款，第一版请人工处理'})
        return convert_refund_to_wechat_original(succeeded[0], operator=operator)

    # 没有任何钱包退款时，直接走微信，不再“先退钻石再扣回”。
    profile = _profile_for_payment(payment)
    _preflight_wechat_refund(payment, profile, amount_to_cents(payment.amount))
    refund = _create_direct_wechat_refund(payment, operator=operator, reason=reason)
    return _submit_wechat_refund(refund, operator=operator)


def _finalize_direct_wechat_refund_success(refund, *, operator=None):
    """微信真正退款成功后再做业务退款副作用，绝不向老板钱包增加钻石。"""
    with transaction.atomic():
        refund = (
            Refund.objects
            .select_for_update(of=('self',))
            .select_related('payment', 'order')
            .get(pk=refund.pk)
        )
        payment = Payment.objects.select_for_update(of=('self',)).get(pk=refund.payment_id)
        refund.payment = payment

        if refund.status != Refund.STATUS_SUCCEEDED:
            refund.status = Refund.STATUS_SUCCEEDED
            refund.failed_reason = ''
            refund.save(update_fields=['status', 'failed_reason', 'updated_at'])

        succeeded_total = (
            Refund.objects
            .filter(payment=payment, status=Refund.STATUS_SUCCEEDED)
            .aggregate(total=Sum('amount'))['total']
            or Decimal('0.00')
        )
        if _qmoney(succeeded_total) >= _qmoney(payment.amount) and payment.status != 'refunded':
            payment.status = 'refunded'
            payment.updated_at = timezone.now()
            payment.save(update_fields=['status', 'updated_at'])

    refund = Refund.objects.select_related('payment', 'order').get(pk=refund.pk)
    effective_operator = _operator_or_none(operator) or refund.created_by

    try:
        from apps.accounts.vip import record_successful_refund
        record_successful_refund(refund, operator=effective_operator)
    except Exception:
        logger.exception('[微信原路退款] VIP/累计消费冲销失败 refund_no=%s', refund.refund_no)

    try:
        from apps.earnings.services import reverse_refund_earnings
        reverse_refund_earnings(refund, operator=effective_operator)
    except Exception:
        logger.exception('[微信原路退款] 陪玩工资冲销失败 refund_no=%s', refund.refund_no)

    return refund


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
    refund_order_id = meta.get('refund_order_id') or refund.refund_no
    _response, order_data = _query_xpay_refund_order(
        openid=profile.openid,
        refund_order_id=refund_order_id,
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
    mode = meta.get('mode') or CASH_MODE_DIRECT

    if remote_status == XPAY_STATUS_REFUNDED:
        if mode == CASH_MODE_DIRECT:
            refund = _finalize_direct_wechat_refund_success(refund, operator=operator)
        meta['status'] = CASH_STATUS_SUCCEEDED
        meta['succeeded_at'] = timezone.now().isoformat()
        third_refund_no = order_data.get('wx_order_id') or refund.third_refund_no or ''
        return _save_cash_meta(refund.pk, meta, third_refund_no=third_refund_no)

    if remote_status in {XPAY_STATUS_CLOSED, XPAY_STATUS_REFUND_FAILED}:
        meta['status'] = CASH_STATUS_FAILED
        meta['failed_at'] = timezone.now().isoformat()
        if mode == CASH_MODE_CONVERTED:
            restored = _restore_wallet_conversion(refund, meta, operator=operator)
            if restored:
                meta['wallet_restored'] = True
                meta['wallet_restored_at'] = timezone.now().isoformat()
            # 原钻石退款依然成立，因此 Refund 仍保持 succeeded。
            return _save_cash_meta(refund.pk, meta)
        return _save_cash_meta(
            refund.pk,
            meta,
            refund_status=Refund.STATUS_FAILED,
            failed_reason='微信虚拟支付原路退款失败',
        )

    meta['status'] = CASH_STATUS_PROCESSING
    return _save_cash_meta(refund.pk, meta)


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
