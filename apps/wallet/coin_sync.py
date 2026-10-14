import hashlib
import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.payments.models import Payment, Refund
from apps.payments.virtualpay import exchange_code_for_session, virtual_env
from apps.payments.xpay_user import user_xpay_post

from .diamonds import (
    LEGACY_COIN_UNITS_PER_YUAN,
    coin_units_per_yuan,
    coin_units_to_yuan,
    format_diamonds,
    qyuan,
    yuan_to_coin_units,
)
from .models import ClientWalletLedger, RechargeOrder


logger = logging.getLogger(__name__)
ZERO = Decimal('0.00')
COIN_MODE = 'short_series_coin'
XPAY_ALREADY_REFUNDED_CODE = 268490005
REMOTE_REFUND_STATUS_KEY = 'wechat_coin_remote_refund_status'
REMOTE_REFUND_ORDER_ID_KEY = 'wechat_coin_remote_refund_order_id'


def _payload_coin_scale(payload):
    raw = dict(payload or {}).get('wechat_coin_units_per_yuan')
    if raw is not None:
        return coin_units_per_yuan(raw)
    # Legacy rows used the displayed diamond count itself as XPay amount.
    return LEGACY_COIN_UNITS_PER_YUAN


def _payment_coin_units(payload):
    payload = dict(payload or {})
    if 'wechat_coin_units' in payload:
        return int(payload.get('wechat_coin_units') or 0)
    # Historical compatibility: before the unit split this field was the
    # integer XPay amount, not merely a display value.
    return int(payload.get('wechat_coin_diamonds') or 0)


def _refund_coin_units(payload):
    payload = dict(payload or {})
    if 'wechat_coin_refund_units' in payload:
        return int(payload.get('wechat_coin_refund_units') or 0)
    return int(payload.get('wechat_coin_refund_diamonds') or 0)


def _coin_recharge_map(profile):
    result = {}
    queryset = RechargeOrder.objects.filter(
        profile=profile,
        status=RechargeOrder.STATUS_CREDITED,
    ).only('recharge_no', 'amount', 'notify_payload')
    for recharge in queryset.iterator():
        payload = dict(recharge.notify_payload or {})
        if payload.get('mode') != COIN_MODE:
            continue
        result[recharge.recharge_no] = (
            qyuan(recharge.amount),
            _payload_coin_scale(payload),
        )
    return result


def _payment_coin_map(profile):
    result = {}
    queryset = Payment.objects.filter(
        order__boss_user__client_profile=profile,
        channel='balance',
        status__in=['paid', 'refunded'],
    ).only('payment_no', 'notify_payload')
    for payment in queryset.iterator():
        payload = dict(payment.notify_payload or {})
        units = _payment_coin_units(payload)
        if units <= 0:
            continue
        scale = _payload_coin_scale(payload)
        result[payment.payment_no] = (
            coin_units_to_yuan(units, units_per_yuan=scale),
            scale,
        )
    return result


def _refund_coin_map(profile):
    from .checkout_refund import surcharge_refund_coin_map
    result = surcharge_refund_coin_map(profile)
    queryset = Refund.objects.filter(
        order__boss_user__client_profile=profile,
        status=Refund.STATUS_SUCCEEDED,
    ).only('refund_no', 'notify_payload')
    for refund in queryset.iterator():
        payload = dict(refund.notify_payload or {})
        if payload.get('wechat_coin_refund_status') != 'succeeded':
            continue
        units = _refund_coin_units(payload)
        if units <= 0:
            continue
        scale = _payload_coin_scale(payload)
        result[refund.refund_no] = (
            coin_units_to_yuan(units, units_per_yuan=scale),
            scale,
        )
    return result


def _consume_lots(lots, amount, *, scale=None):
    remaining = qyuan(amount)
    if remaining <= ZERO:
        return ZERO
    consumed = ZERO
    for lot in lots:
        if remaining <= ZERO:
            break
        if scale is not None and lot['scale'] != scale:
            continue
        available = qyuan(lot['amount'])
        if available <= ZERO:
            continue
        take = min(available, remaining)
        lot['amount'] = qyuan(available - take)
        remaining = qyuan(remaining - take)
        consumed = qyuan(consumed + take)
    return consumed


def coin_backed_wallet_buckets(profile, *, with_lots=False):
    """Replay local wallet history into remaining coin-backed RMB by XPay scale.

    Keeping scales separate prevents a historical 10-units/RMB coin balance
    from being silently mixed with a new 100-units/RMB balance.  No historical
    order amount is rewritten; this is purely an interpretation of immutable
    ledger metadata.
    """
    recharge_map = _coin_recharge_map(profile)
    payment_map = _payment_coin_map(profile)
    refund_map = _refund_coin_map(profile)
    from .spend_models import WalletSpendAttempt
    shared_map = {a.external_id: a.source_snapshot for a in WalletSpendAttempt.objects.filter(
        wallet__profile=profile, status='completed').only('external_id', 'source_snapshot')}

    running = ZERO
    lots = []
    entries = ClientWalletLedger.objects.filter(wallet__profile=profile).order_by('id')
    for entry in entries.iterator():
        amount = qyuan(entry.amount)
        if entry.entry_type in ClientWalletLedger.INTERNAL_ENTRY_TYPES:
            continue

        if entry.entry_type == ClientWalletLedger.TYPE_RECHARGE:
            running = qyuan(running + amount)
            source = recharge_map.get(str(entry.reference_id or ''))
            if source:
                source_amount, scale = source
                # Ledger credit is the actual spendable value (iOS may credit
                # less than paid). Never relabel pre-existing manual funds.
                lots.append({'scale': scale, 'amount': qyuan(min(source_amount, max(ZERO, amount))),
                    'ledger_id': entry.pk, 'reference_id': entry.reference_id, 'entry_type': entry.entry_type,
                    'source_paid_amount': str(source_amount), 'source_credit_amount': str(amount)})
            backed = qyuan(sum((lot['amount'] for lot in lots), ZERO))
            if backed > running:
                _consume_lots(lots, backed - running)
            continue

        if entry.entry_type == ClientWalletLedger.TYPE_REFUND_IN:
            running = qyuan(running + amount)
            source = refund_map.get(str(entry.reference_id or ''))
            if source:
                source_amount, scale = source
                # Ledger credit is the actual spendable value (iOS may credit
                # less than paid). Never relabel pre-existing manual funds.
                lots.append({'scale': scale, 'amount': qyuan(min(source_amount, max(ZERO, amount))),
                    'ledger_id': entry.pk, 'reference_id': entry.reference_id, 'entry_type': entry.entry_type,
                    'source_paid_amount': str(source_amount), 'source_credit_amount': str(amount)})
            backed = qyuan(sum((lot['amount'] for lot in lots), ZERO))
            if backed > running:
                _consume_lots(lots, backed - running)
            continue

        if amount >= ZERO:
            running = qyuan(running + amount)
            continue

        spend = -amount
        if entry.entry_type == 'shared_spend':
            source = shared_map.get(str(entry.reference_id or ''))
            if source is None:
                raise ValidationError({'code': 'SOURCE_AUDIT_REQUIRED', 'detail': '共享消费缺少来源证明'})
            _consume_lots(lots, Decimal(source['coin_amount']), scale=int(source['scale']))
        elif entry.entry_type == ClientWalletLedger.TYPE_ORDER_PAYMENT:
            explicit = payment_map.get(str(entry.reference_id or ''))
            if explicit:
                explicit_amount, explicit_scale = explicit
                _consume_lots(lots, explicit_amount, scale=explicit_scale)
        else:
            backed = qyuan(sum((lot['amount'] for lot in lots), ZERO))
            non_coin = max(ZERO, running - backed)
            overflow = max(ZERO, spend - non_coin)
            _consume_lots(lots, overflow)

        running = qyuan(max(ZERO, running - spend))
        backed = qyuan(sum((lot['amount'] for lot in lots), ZERO))
        if backed > running:
            _consume_lots(lots, backed - running)

    if with_lots:
        return [lot for lot in lots if lot['amount'] > ZERO]
    buckets = {}
    for lot in lots:
        amount = qyuan(lot['amount'])
        if amount <= ZERO:
            continue
        scale = lot['scale']
        buckets[scale] = qyuan(buckets.get(scale, ZERO) + amount)
    return buckets


def coin_backed_wallet_amount(profile):
    return qyuan(sum(coin_backed_wallet_buckets(profile).values(), ZERO))


def _coin_payment_order_id(order_no, coin_units, scale):
    digest = hashlib.sha256(
        f'{order_no}:{coin_units}:{scale}'.encode('utf-8')
    ).hexdigest()[:28].upper()
    return f'CP{digest}'


def _coin_remote_refund_order_id(payment, pay_order_id):
    digest = hashlib.sha256(
        f'{payment.payment_no}:{pay_order_id}:full-coin-refund'.encode('utf-8')
    ).hexdigest()[:28].upper()
    return f'CR{digest}'


def _request_session(profile, code):
    if not code:
        raise ValidationError({'detail': '当前钱包包含微信官方钻石，请重新登录后再支付'})
    openid, session_key = exchange_code_for_session(code)
    if openid != profile.openid:
        raise ValidationError({'detail': '本次微信登录账号与当前账号不一致'})
    return session_key


def query_remote_coin_balance(profile, session_key, user_ip='127.0.0.1'):
    """Return the raw integer XPay balance; interpretation uses the active scale."""
    response = user_xpay_post('/xpay/query_user_balance', {
        'openid': profile.openid,
        'env': virtual_env(),
        'user_ip': user_ip or '127.0.0.1',
    }, session_key)
    try:
        balance = int(response.get('balance') or 0)
    except (TypeError, ValueError):
        raise ValidationError({'detail': '微信官方钻石余额返回异常，请稍后重试'})
    if balance < 0:
        raise ValidationError({'detail': '微信官方钻石余额返回异常，请稍后重试'})
    return balance, response


def has_pending_coin_refunds(profile):
    from .checkout_refund import pending_checkout_refunds
    if pending_checkout_refunds(profile):
        return True
    queryset = Refund.objects.filter(
        order__boss_user__client_profile=profile,
        status=Refund.STATUS_SUCCEEDED,
        payment__channel='balance',
    ).only('notify_payload')
    for refund in queryset.iterator():
        payload = dict(refund.notify_payload or {})
        if _refund_coin_units(payload) > 0 and payload.get('wechat_coin_refund_status') != 'succeeded':
            return True
    return False


def _mark_local_refund_coin_synced(refund, payload, response, remote_order_id, *, reused_remote_refund=False):
    payload['wechat_coin_refund_status'] = 'succeeded'
    payload['wechat_coin_refund_response'] = response
    payload['wechat_coin_remote_refund_order_id'] = remote_order_id
    if reused_remote_refund:
        payload['wechat_coin_refund_reused_remote_full_cancel'] = True
    Refund.objects.filter(pk=refund.pk).update(notify_payload=payload)


def sync_pending_coin_refunds(profile, session_key, user_ip='127.0.0.1'):
    """Synchronize local refunds while issuing at most one remote cancel per pay.

    WeChat's 268490005 states that an order already refunded by
    ``cancel_currency_pay`` cannot be refunded again.  The first local refund
    therefore restores the whole original coin-backed spend remotely.  Later
    partial local refunds only unlock their corresponding local coin-backed
    share from that already-restored remote pool.
    """
    from django.db import connection
    if connection.in_atomic_block:
        raise RuntimeError('Remote coin refund requires committed local refund')
    queryset = Refund.objects.filter(
        order__boss_user__client_profile=profile,
        status=Refund.STATUS_SUCCEEDED,
        payment__channel='balance',
    ).select_related('payment').order_by('id')
    from .checkout_refund import sync_checkout_refunds
    synced = sync_checkout_refunds(profile, session_key)
    for refund in queryset.iterator():
        payload = dict(refund.notify_payload or {})
        if payload.get('checkout_refund_id'):
            continue  # Sole owner is the durable combined refund intent.
        refund_units = _refund_coin_units(payload)
        if refund_units <= 0 or payload.get('wechat_coin_refund_status') == 'succeeded':
            continue

        payment = Payment.objects.filter(pk=refund.payment_id).only(
            'id', 'payment_no', 'notify_payload'
        ).first()
        if not payment:
            continue
        payment_payload = dict(payment.notify_payload or {})
        pay_order_id = str(payment_payload.get('wechat_coin_order_id') or '')
        total_units = _payment_coin_units(payment_payload)
        if not pay_order_id or total_units <= 0:
            continue

        remote_order_id = str(
            payment_payload.get(REMOTE_REFUND_ORDER_ID_KEY)
            or _coin_remote_refund_order_id(payment, pay_order_id)
        )

        if payment_payload.get(REMOTE_REFUND_STATUS_KEY) == 'succeeded':
            _mark_local_refund_coin_synced(
                refund,
                payload,
                payment_payload.get('wechat_coin_remote_refund_response') or {'reused': True},
                remote_order_id,
                reused_remote_refund=True,
            )
            synced += 1
            continue

        response = user_xpay_post('/xpay/cancel_currency_pay', {
            'openid': profile.openid,
            'env': virtual_env(),
            'user_ip': user_ip or '127.0.0.1',
            'pay_order_id': pay_order_id,
            'order_id': remote_order_id,
            'amount': total_units,
        }, session_key, extra_success_codes={XPAY_ALREADY_REFUNDED_CODE})

        payment_payload[REMOTE_REFUND_STATUS_KEY] = 'succeeded'
        payment_payload[REMOTE_REFUND_ORDER_ID_KEY] = remote_order_id
        payment_payload['wechat_coin_remote_refund_amount_units'] = total_units
        payment_payload['wechat_coin_remote_refund_response'] = response
        payment_payload['wechat_coin_remote_refunded_at'] = timezone.now().isoformat()
        Payment.objects.filter(pk=payment.pk).update(notify_payload=payment_payload)

        _mark_local_refund_coin_synced(
            refund,
            payload,
            response,
            remote_order_id,
        )
        synced += 1
    return synced


def allocate_refund_coin_amount(refund):
    """Allocate the exact coin-backed share of one successful local refund."""
    if not refund.payment_id or refund.payment.channel != 'balance':
        return 0

    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=refund.payment_id)
        locked_refund = Refund.objects.select_for_update().get(pk=refund.pk)
        payment_payload = dict(payment.notify_payload or {})
        total_units = _payment_coin_units(payment_payload)
        if total_units <= 0:
            return 0
        scale = _payload_coin_scale(payment_payload)

        payload = dict(locked_refund.notify_payload or {})
        if 'wechat_coin_refund_units' in payload:
            return int(payload.get('wechat_coin_refund_units') or 0)
        # Historical rows may already have the legacy allocation field.
        if 'wechat_coin_refund_diamonds' in payload and 'wechat_coin_units' not in payment_payload:
            return int(payload.get('wechat_coin_refund_diamonds') or 0)

        allocated_before = 0
        previous_refunds = payment.refunds.exclude(pk=locked_refund.pk).filter(
            status=Refund.STATUS_SUCCEEDED,
        ).only('notify_payload')
        for previous in previous_refunds:
            allocated_before += _refund_coin_units(previous.notify_payload)

        available = max(0, total_units - allocated_before)
        requested_units = yuan_to_coin_units(
            locked_refund.amount,
            units_per_yuan=scale,
        )
        allocated = min(available, requested_units)
        allocated_yuan = coin_units_to_yuan(allocated, units_per_yuan=scale) if allocated else ZERO
        payload['wechat_coin_refund_units'] = allocated
        payload['wechat_coin_units_per_yuan'] = scale
        payload['wechat_coin_refund_diamonds'] = format_diamonds(allocated_yuan)
        payload['wechat_coin_refund_status'] = 'pending' if allocated > 0 else 'not_required'
        Refund.objects.filter(pk=locked_refund.pk).update(notify_payload=payload)
        refund.notify_payload = payload
        return allocated


def _compatible_backing_amount(profile, scale):
    buckets = coin_backed_wallet_buckets(profile)
    incompatible = qyuan(sum(
        (amount for item_scale, amount in buckets.items() if item_scale != scale),
        ZERO,
    ))
    if incompatible > ZERO:
        raise ValidationError({
            'detail': (
                '钱包仍含旧版微信钻石余额，结算单位与当前配置不一致；'
                '为避免倍率误扣已暂停支付，请管理员先完成旧余额迁移或保持原结算配置'
            )
        })
    return qyuan(buckets.get(scale, ZERO))


def prepare_coin_spend(profile, amount, code, user_ip='127.0.0.1'):
    """Retired: historical callers must not reconstruct a non-durable debit."""
    raise ValidationError({'code': 'LEGACY_COIN_REVIEW_REQUIRED',
        'detail': '旧扣款入口已停用，请使用持久订单消费；历史不确定扣款须人工核验'})


def execute_coin_spend(profile, order, metadata):
    raise ValidationError({'code': 'LEGACY_COIN_REVIEW_REQUIRED',
        'detail': '不能在没有持久attempt的情况下重新发起微信扣款'})
