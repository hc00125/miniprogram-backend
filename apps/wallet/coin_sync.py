import hashlib
from decimal import Decimal

from rest_framework.exceptions import ValidationError

from apps.payments.models import Payment, Refund
from apps.payments.virtualpay import exchange_code_for_session, virtual_env
from apps.payments.xpay_user import user_xpay_post

from .diamonds import diamonds_to_yuan, qyuan, yuan_to_diamonds
from .models import ClientWalletLedger, RechargeOrder


ZERO = Decimal('0.00')
COIN_MODE = 'short_series_coin'


def _coin_recharge_map(profile):
    result = {}
    queryset = RechargeOrder.objects.filter(
        profile=profile,
        status=RechargeOrder.STATUS_CREDITED,
    ).only('recharge_no', 'amount', 'notify_payload')
    for recharge in queryset.iterator():
        payload = dict(recharge.notify_payload or {})
        if payload.get('mode') == COIN_MODE:
            result[recharge.recharge_no] = qyuan(recharge.amount)
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
        diamonds = int(payload.get('wechat_coin_diamonds') or 0)
        if diamonds > 0:
            result[payment.payment_no] = diamonds_to_yuan(diamonds)
    return result


def _refund_coin_map(profile):
    result = {}
    queryset = Refund.objects.filter(
        order__boss_user__client_profile=profile,
        status=Refund.STATUS_SUCCEEDED,
    ).only('refund_no', 'notify_payload')
    for refund in queryset.iterator():
        payload = dict(refund.notify_payload or {})
        if payload.get('wechat_coin_refund_status') != 'succeeded':
            continue
        diamonds = int(payload.get('wechat_coin_refund_diamonds') or 0)
        if diamonds > 0:
            result[refund.refund_no] = diamonds_to_yuan(diamonds)
    return result


def coin_backed_wallet_amount(profile):
    """计算当前本地钱包中仍由微信官方 coin 支撑的人民币等值余额。"""
    recharge_map = _coin_recharge_map(profile)
    payment_map = _payment_coin_map(profile)
    refund_map = _refund_coin_map(profile)

    running = ZERO
    coin_backed = ZERO
    entries = ClientWalletLedger.objects.filter(wallet__profile=profile).order_by('id')
    for entry in entries.iterator():
        amount = qyuan(entry.amount)
        if entry.entry_type in ClientWalletLedger.INTERNAL_ENTRY_TYPES:
            continue

        if entry.entry_type == ClientWalletLedger.TYPE_RECHARGE:
            running = qyuan(running + amount)
            coin_backed = qyuan(coin_backed + recharge_map.get(str(entry.reference_id or ''), ZERO))
            coin_backed = min(coin_backed, running)
            continue

        if entry.entry_type == ClientWalletLedger.TYPE_REFUND_IN:
            running = qyuan(running + amount)
            coin_backed = qyuan(coin_backed + refund_map.get(str(entry.reference_id or ''), ZERO))
            coin_backed = min(coin_backed, running)
            continue

        if amount >= ZERO:
            running = qyuan(running + amount)
            coin_backed = min(coin_backed, running)
            continue

        spend = -amount
        if entry.entry_type == ClientWalletLedger.TYPE_ORDER_PAYMENT:
            explicit_coin = payment_map.get(str(entry.reference_id or ''), ZERO)
            coin_backed = qyuan(max(ZERO, coin_backed - min(explicit_coin, coin_backed)))
        else:
            non_coin = max(ZERO, running - coin_backed)
            overflow = max(ZERO, spend - non_coin)
            coin_backed = qyuan(max(ZERO, coin_backed - overflow))
        running = qyuan(max(ZERO, running - spend))
        coin_backed = min(coin_backed, running)

    return qyuan(max(ZERO, coin_backed))


def _coin_payment_order_id(order_no, diamonds):
    digest = hashlib.sha256(f'{order_no}:{diamonds}'.encode('utf-8')).hexdigest()[:28].upper()
    return f'CP{digest}'


def _request_session(profile, code):
    if not code:
        raise ValidationError({'detail': '当前钱包包含微信官方钻石，请重新登录后再支付'})
    openid, session_key = exchange_code_for_session(code)
    if openid != profile.openid:
        raise ValidationError({'detail': '本次微信登录账号与当前账号不一致'})
    return session_key


def query_remote_coin_balance(profile, session_key, user_ip='127.0.0.1'):
    """查询微信官方 coin 余额，用于本地账本扣款前的双账校验。"""
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
    queryset = Refund.objects.filter(
        order__boss_user__client_profile=profile,
        status=Refund.STATUS_SUCCEEDED,
        payment__channel='balance',
    ).only('notify_payload')
    for refund in queryset.iterator():
        payload = dict(refund.notify_payload or {})
        if int(payload.get('wechat_coin_refund_diamonds') or 0) > 0 and payload.get('wechat_coin_refund_status') != 'succeeded':
            return True
    return False


def sync_pending_coin_refunds(profile, session_key, user_ip='127.0.0.1'):
    queryset = Refund.objects.filter(
        order__boss_user__client_profile=profile,
        status=Refund.STATUS_SUCCEEDED,
        payment__channel='balance',
    ).select_related('payment').order_by('id')
    synced = 0
    for refund in queryset.iterator():
        payload = dict(refund.notify_payload or {})
        diamonds = int(payload.get('wechat_coin_refund_diamonds') or 0)
        if diamonds <= 0 or payload.get('wechat_coin_refund_status') == 'succeeded':
            continue
        payment_payload = dict(refund.payment.notify_payload or {})
        pay_order_id = str(payment_payload.get('wechat_coin_order_id') or '')
        if not pay_order_id:
            continue

        response = user_xpay_post('/xpay/cancel_currency_pay', {
            'openid': profile.openid,
            'env': virtual_env(),
            'user_ip': user_ip or '127.0.0.1',
            'pay_order_id': pay_order_id,
            'order_id': refund.refund_no,
            'amount': diamonds,
        }, session_key)
        payload['wechat_coin_refund_status'] = 'succeeded'
        payload['wechat_coin_refund_response'] = response
        Refund.objects.filter(pk=refund.pk).update(notify_payload=payload)
        synced += 1
    return synced


def allocate_refund_coin_amount(refund):
    """给成功的本地钱包退款分配应同步回微信 coin 的份额。"""
    payment = refund.payment
    if not payment or payment.channel != 'balance':
        return 0
    payment_payload = dict(payment.notify_payload or {})
    total_coin = int(payment_payload.get('wechat_coin_diamonds') or 0)
    if total_coin <= 0:
        return 0

    payload = dict(refund.notify_payload or {})
    if 'wechat_coin_refund_diamonds' in payload:
        return int(payload.get('wechat_coin_refund_diamonds') or 0)

    allocated_before = 0
    for previous in payment.refunds.exclude(pk=refund.pk).filter(
        status=Refund.STATUS_SUCCEEDED,
    ).only('notify_payload'):
        previous_payload = dict(previous.notify_payload or {})
        allocated_before += int(previous_payload.get('wechat_coin_refund_diamonds') or 0)

    available = max(0, total_coin - allocated_before)
    try:
        refund_diamonds = yuan_to_diamonds(refund.amount)
    except Exception:
        refund_diamonds = 0
    allocated = min(available, refund_diamonds)
    payload['wechat_coin_refund_diamonds'] = allocated
    payload['wechat_coin_refund_status'] = 'pending' if allocated > 0 else 'not_required'
    Refund.objects.filter(pk=refund.pk).update(notify_payload=payload)
    refund.notify_payload = payload
    return allocated


def prepare_coin_spend(profile, amount, code, user_ip='127.0.0.1'):
    """先同步 pending coin 退款，再准备本次官方 coin 扣减。"""
    amount = qyuan(amount)
    backed = coin_backed_wallet_amount(profile)
    pending_refunds = has_pending_coin_refunds(profile)
    spend_yuan = min(amount, backed)

    if spend_yuan <= ZERO and not pending_refunds:
        return {
            'wechat_coin_diamonds': 0,
            'wechat_coin_amount_yuan': '0.00',
            'wechat_coin_status': 'not_required',
        }

    session_key = _request_session(profile, code)
    if pending_refunds:
        sync_pending_coin_refunds(profile, session_key, user_ip)
        backed = coin_backed_wallet_amount(profile)
        spend_yuan = min(amount, backed)

    if spend_yuan <= ZERO:
        return {
            'wechat_coin_diamonds': 0,
            'wechat_coin_amount_yuan': '0.00',
            'wechat_coin_status': 'not_required',
        }

    diamonds = yuan_to_diamonds(spend_yuan)
    remote_balance, balance_response = query_remote_coin_balance(profile, session_key, user_ip)
    if remote_balance < diamonds:
        raise ValidationError({
            'detail': (
                f'钻石账本需要扣除{diamonds}钻石，但微信官方余额仅有{remote_balance}钻石；'
                '已停止本次支付，请刷新钱包或联系客服核对'
            )
        })

    order_id = _coin_payment_order_id(profile.user_id, diamonds)
    return {
        '_session_key': session_key,
        '_user_ip': user_ip or '127.0.0.1',
        'wechat_coin_diamonds': diamonds,
        'wechat_coin_amount_yuan': str(spend_yuan),
        'wechat_coin_status': 'prepared',
        'wechat_coin_order_id': order_id,
        'wechat_coin_balance_before': remote_balance,
        'wechat_coin_balance_query': balance_response,
    }


def execute_coin_spend(profile, order, metadata):
    diamonds = int(metadata.get('wechat_coin_diamonds') or 0)
    if diamonds <= 0:
        return metadata
    session_key = metadata.pop('_session_key', '')
    user_ip = metadata.pop('_user_ip', '127.0.0.1')
    order_id = _coin_payment_order_id(order.order_no, diamonds)
    payitem = [{
        'productid': f'order_{order.order_no}'[:64],
        'unit_price': diamonds,
        'quantity': 1,
    }]
    response = user_xpay_post('/xpay/currency_pay', {
        'openid': profile.openid,
        'env': virtual_env(),
        'user_ip': user_ip,
        'amount': diamonds,
        'order_id': order_id,
        'payitem': __import__('json').dumps(payitem, ensure_ascii=False, separators=(',', ':')),
        'remark': f'订单{order.order_no}钻石支付'[:64],
    }, session_key)
    metadata['wechat_coin_status'] = 'succeeded'
    metadata['wechat_coin_order_id'] = order_id
    metadata['wechat_coin_response'] = response
    try:
        metadata['wechat_coin_balance_after'] = int(response.get('balance'))
    except (TypeError, ValueError):
        pass
    return metadata
