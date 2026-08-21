from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.payments.services import amount_to_cents
from apps.payments.virtualpay import (
    PAID_XPAY_STATUSES,
    VIRTUAL_PACKAGE_PREFIX,
    VirtualPaymentError,
    compact_json,
    exchange_code_for_session,
    hmac_sha256_hex,
    validate_configuration,
    virtual_env,
    xpay_post,
)

from .diamonds import (
    DIAMONDS_PER_YUAN,
    LEGACY_COIN_UNITS_PER_YUAN,
    coin_units_per_yuan,
    format_diamonds,
    qyuan,
    yuan_to_coin_units,
)
from .models import RechargeOrder
from .services import generate_recharge_no, mark_recharge_paid, query_recharge as legacy_query_recharge


VIRTUAL_MODE_COIN = 'short_series_coin'
RECHARGE_ATTACH = 'wallet_coin_recharge'
RECHARGE_EXPIRE_MINUTES = 10
HUNDRED = Decimal('100.00')
CENT = Decimal('0.01')


def normalize_platform(value):
    value = str(value or '').strip().lower()
    if value in {'ios', 'iphone', 'ipad', 'ipod'}:
        return 'ios'
    if value == 'android':
        return 'android'
    if value in {'windows', 'win32', 'win'}:
        return 'windows'
    if value in {'harmony', 'harmonyos', 'ohos'}:
        return 'harmony'
    if value == 'devtools':
        return 'devtools'
    return value or 'other'


def _platform_fee_percent(platform):
    from apps.earnings.platform_fees import platform_fee_percent

    return platform_fee_percent(platform)


def recharge_limits(platform):
    platform = normalize_platform(platform)
    minimum = Decimal('1.00') if platform == 'ios' else Decimal(
        str(getattr(settings, 'WECHAT_VIRTUALPAY_RECHARGE_MIN_YUAN', '0.01'))
    )
    maximum = Decimal(str(getattr(settings, 'WECHAT_VIRTUALPAY_RECHARGE_MAX_YUAN', '5000.00')))
    return qyuan(minimum), qyuan(maximum)


def validate_recharge_amount(value, platform):
    amount = qyuan(value)
    minimum, maximum = recharge_limits(platform)
    if amount < minimum:
        if normalize_platform(platform) == 'ios':
            raise ValidationError({'amount_yuan': 'iOS Apple 虚拟支付单笔最低充值金额为1元'})
        raise ValidationError({'amount_yuan': f'单笔最低充值金额为{minimum:.2f}元'})
    if amount > maximum:
        raise ValidationError({'amount_yuan': f'单笔充值不能超过{maximum:.2f}元'})
    # Validate against the integer XPay settlement scale.  At 100 units/RMB
    # every RMB cent is exact, while the user still sees 1 RMB = 10 diamonds.
    yuan_to_coin_units(amount)
    return amount


def _fee_payload(amount, platform):
    fee_percent = _platform_fee_percent(platform)
    fee_yuan = (amount * fee_percent / HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)
    settlement_yuan = (amount - fee_yuan).quantize(CENT, rounding=ROUND_HALF_UP)
    return {
        'client_platform': normalize_platform(platform),
        'platform_fee_percent': str(fee_percent),
        'estimated_platform_fee_yuan': str(fee_yuan),
        'estimated_settlement_yuan': str(settlement_yuan),
    }


def _is_coin_recharge(recharge):
    return dict(recharge.notify_payload or {}).get('mode') == VIRTUAL_MODE_COIN


def _checkout_order_no(recharge):
    return str(dict(recharge.notify_payload or {}).get('checkout_order_no') or '')


def _same_recharge_purpose(recharge, checkout_order_no=''):
    return _checkout_order_no(recharge) == str(checkout_order_no or '')


def _recharge_coin_scale(recharge):
    stored = dict(recharge.notify_payload or {})
    raw = stored.get('wechat_coin_units_per_yuan')
    if raw is not None:
        return coin_units_per_yuan(raw)
    # Rows created before the split between display diamonds and settlement
    # units used one XPay coin per displayed diamond (10 units/RMB).
    return LEGACY_COIN_UNITS_PER_YUAN


def _build_coin_payment_payload(recharge, env, session_key):
    stored = dict(recharge.notify_payload or {})
    scale = _recharge_coin_scale(recharge)
    coin_units = yuan_to_coin_units(recharge.amount, units_per_yuan=scale)
    diamonds = format_diamonds(recharge.amount)
    sign_data = compact_json({
        'offerId': str(settings.WECHAT_VIRTUALPAY_OFFER_ID),
        'buyQuantity': coin_units,
        'env': env,
        'currencyType': 'CNY',
        'outTradeNo': recharge.recharge_no,
        'attach': RECHARGE_ATTACH,
    })
    pay_sig = hmac_sha256_hex(
        settings.WECHAT_VIRTUALPAY_APP_KEY,
        f'requestVirtualPayment&{sign_data}',
    )
    signature = hmac_sha256_hex(session_key, sign_data)

    bridge_payload = {
        'signData': sign_data,
        'paySig': pay_sig,
        'signature': signature,
        'mode': VIRTUAL_MODE_COIN,
        'payment_no': recharge.recharge_no,
        'env': env,
    }
    encoded_payload = quote(compact_json(bridge_payload), safe='')
    return {
        'signData': sign_data,
        'paySig': pay_sig,
        'signature': signature,
        'mode': VIRTUAL_MODE_COIN,
        'timeStamp': '0',
        'nonceStr': 'virtual-payment',
        'package': f'{VIRTUAL_PACKAGE_PREFIX}{encoded_payload}',
        'signType': 'VIRTUAL',
        'paySign': pay_sig,
        'payment_no': recharge.recharge_no,
        'recharge_no': recharge.recharge_no,
        'order_no': stored.get('checkout_order_no') or None,
        'checkout_order_no': stored.get('checkout_order_no') or None,
        'amount': str(qyuan(recharge.amount)),
        'pay_amount_yuan': str(qyuan(recharge.amount)),
        'diamonds': diamonds,
        'diamonds_per_yuan': DIAMONDS_PER_YUAN,
        'wechat_coin_units': coin_units,
        'wechat_coin_units_per_yuan': scale,
        'status': recharge.status,
        'virtual': True,
        'virtual_env': env,
        'client_platform': stored.get('client_platform', 'other'),
        'platform_fee_percent': stored.get('platform_fee_percent'),
        'estimated_platform_fee_yuan': stored.get('estimated_platform_fee_yuan'),
        'estimated_settlement_yuan': stored.get('estimated_settlement_yuan'),
    }


def _build_mock_payload(recharge):
    stored = dict(recharge.notify_payload or {})
    scale = _recharge_coin_scale(recharge)
    return {
        'timeStamp': str(int(timezone.now().timestamp())),
        'nonceStr': recharge.recharge_no,
        'package': f'prepay_id=mock_{recharge.recharge_no}',
        'signType': 'RSA',
        'paySign': recharge.recharge_no,
        'payment_no': recharge.recharge_no,
        'recharge_no': recharge.recharge_no,
        'order_no': stored.get('checkout_order_no') or None,
        'checkout_order_no': stored.get('checkout_order_no') or None,
        'amount': str(qyuan(recharge.amount)),
        'pay_amount_yuan': str(qyuan(recharge.amount)),
        'diamonds': format_diamonds(recharge.amount),
        'diamonds_per_yuan': DIAMONDS_PER_YUAN,
        'wechat_coin_units': yuan_to_coin_units(recharge.amount, units_per_yuan=scale),
        'wechat_coin_units_per_yuan': scale,
        'status': recharge.status,
        'mock': True,
        'client_platform': stored.get('client_platform', 'other'),
        'platform_fee_percent': stored.get('platform_fee_percent'),
        'estimated_platform_fee_yuan': stored.get('estimated_platform_fee_yuan'),
        'estimated_settlement_yuan': stored.get('estimated_settlement_yuan'),
    }


def _create_mock_recharge(profile, amount, platform, checkout_order_no=''):
    now = timezone.now()
    candidates = (
        RechargeOrder.objects
        .filter(
            profile=profile,
            product__isnull=True,
            amount=amount,
            channel=RechargeOrder.CHANNEL_MOCK,
            status=RechargeOrder.STATUS_PAYING,
            expires_at__gt=now,
        )
        .order_by('-created_at')
    )
    existing = next((item for item in candidates if _is_coin_recharge(item) and _same_recharge_purpose(item, checkout_order_no)), None)
    if existing:
        return existing, _build_mock_payload(existing)

    scale = coin_units_per_yuan()
    payload = {
        'recharge': True,
        'mock': True,
        'mode': VIRTUAL_MODE_COIN,
        'checkout_order_no': str(checkout_order_no or ''),
        'wechat_coin_units_per_yuan': scale,
        **_fee_payload(amount, platform),
    }
    recharge = RechargeOrder.objects.create(
        recharge_no=generate_recharge_no(),
        profile=profile,
        product=None,
        amount=amount,
        channel=RechargeOrder.CHANNEL_MOCK,
        status=RechargeOrder.STATUS_PAYING,
        expires_at=now + timedelta(minutes=RECHARGE_EXPIRE_MINUTES),
        notify_payload=payload,
    )
    return recharge, _build_mock_payload(recharge)


def create_coin_recharge(user, amount_yuan, code, platform, checkout_order_no=''):
    profile = getattr(user, 'client_profile', None)
    if not profile or not profile.openid:
        raise PermissionDenied('当前账号未绑定微信 openid，请重新登录')

    platform = normalize_platform(platform)
    amount = validate_recharge_amount(amount_yuan, platform)
    checkout_order_no = str(checkout_order_no or '')

    if settings.ENABLE_MOCK_PAYMENT and not settings.WECHAT_VIRTUALPAY_ENABLED:
        return _create_mock_recharge(profile, amount, platform, checkout_order_no)

    env = validate_configuration()
    if platform == 'ios' and env != 0:
        raise ValidationError({'detail': 'iOS Apple 虚拟支付不支持沙箱，请将 WECHAT_VIRTUALPAY_ENV 设为0后使用真机正式环境测试'})

    openid, session_key = exchange_code_for_session(code)
    if openid != profile.openid:
        raise PermissionDenied('本次微信登录账号与当前账号不一致')

    now = timezone.now()
    candidates = (
        RechargeOrder.objects
        .filter(
            profile=profile,
            product__isnull=True,
            amount=amount,
            channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
            status=RechargeOrder.STATUS_PAYING,
        )
        .order_by('-created_at')
    )
    existing = next((item for item in candidates if _is_coin_recharge(item) and _same_recharge_purpose(item, checkout_order_no)), None)
    if existing:
        stored_platform = normalize_platform(dict(existing.notify_payload or {}).get('client_platform'))
        if stored_platform == platform and (not existing.expires_at or existing.expires_at > now):
            return existing, _build_coin_payment_payload(existing, env, session_key)
        if existing.expires_at and existing.expires_at <= now:
            # query_coin_recharge raises on an unknown/network state, so this
            # block can only close after a successful query proved it unpaid.
            synced = query_coin_recharge(existing.recharge_no, user)
            if synced.status in {RechargeOrder.STATUS_PAID, RechargeOrder.STATUS_CREDITED}:
                raise ValidationError({'detail': '该充值单已支付并入账，请勿重复充值'})
            RechargeOrder.objects.filter(pk=existing.pk, status=RechargeOrder.STATUS_PAYING).update(
                status=RechargeOrder.STATUS_CLOSED,
                updated_at=timezone.now(),
            )

    scale = coin_units_per_yuan()
    payload = {
        'recharge': True,
        'mode': VIRTUAL_MODE_COIN,
        'env': env,
        'checkout_order_no': checkout_order_no,
        'wechat_coin_units_per_yuan': scale,
        **_fee_payload(amount, platform),
    }
    with transaction.atomic():
        recharge = RechargeOrder.objects.create(
            recharge_no=generate_recharge_no(),
            profile=profile,
            product=None,
            amount=amount,
            channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
            status=RechargeOrder.STATUS_PAYING,
            expires_at=now + timedelta(minutes=RECHARGE_EXPIRE_MINUTES),
            notify_payload=payload,
        )

    return recharge, _build_coin_payment_payload(recharge, env, session_key)


def query_coin_recharge(recharge_no, user):
    if not user or not getattr(user, 'is_authenticated', False):
        raise PermissionDenied('请先登录')
    recharge = (
        RechargeOrder.objects
        .select_related('profile', 'profile__user')
        .filter(recharge_no=recharge_no, profile__user=user)
        .first()
    )
    if not recharge:
        raise ValidationError({'detail': '充值单不存在'})

    if not _is_coin_recharge(recharge):
        return legacy_query_recharge(recharge_no, user)

    if recharge.status == RechargeOrder.STATUS_CREDITED:
        return recharge
    if recharge.channel == RechargeOrder.CHANNEL_MOCK:
        return recharge

    validate_configuration()
    response = xpay_post('/xpay/query_order', {
        'openid': recharge.profile.openid,
        'env': virtual_env(),
        'order_id': recharge.recharge_no,
    })
    order_data = response.get('order') or {}
    xpay_status = int(order_data.get('status') or 0)
    order_fee = int(order_data.get('order_fee') or 0)
    paid_fee = int(order_data.get('paid_fee') or 0)

    with transaction.atomic():
        recharge = RechargeOrder.objects.select_for_update().get(pk=recharge.pk)
        expected_fen = amount_to_cents(recharge.amount)
        payload = dict(recharge.notify_payload or {})
        payload['query_order'] = order_data
        recharge.notify_payload = payload
        recharge.third_trade_no = (
            order_data.get('wx_order_id')
            or order_data.get('wxpay_order_id')
            or recharge.third_trade_no
        )
        recharge.save(update_fields=['notify_payload', 'third_trade_no', 'updated_at'])

        if xpay_status in PAID_XPAY_STATUSES:
            if order_fee != expected_fen or paid_fee != expected_fen:
                raise VirtualPaymentError('微信虚拟支付充值金额校验失败，请联系管理员处理')
            recharge = mark_recharge_paid(
                recharge,
                recharge.third_trade_no or recharge.recharge_no,
                payload,
            )

    return recharge


def reconcile_coin_recharge_order(recharge):
    if not _is_coin_recharge(recharge):
        from .services import reconcile_recharge_order as legacy_reconcile
        return legacy_reconcile(recharge)

    # Defense in depth: even if another caller accidentally passes a fresh
    # PAYING recharge, never close it before its local expiry window.
    if not recharge.expires_at or recharge.expires_at > timezone.now():
        return 'skipped'

    try:
        response = xpay_post('/xpay/query_order', {
            'openid': recharge.profile.openid,
            'env': virtual_env(),
            'order_id': recharge.recharge_no,
        })
    except Exception:
        return 'skipped'

    order_data = response.get('order') or {}
    xpay_status = int(order_data.get('status') or 0)
    order_fee = int(order_data.get('order_fee') or 0)
    paid_fee = int(order_data.get('paid_fee') or 0)
    expected_fen = amount_to_cents(recharge.amount)

    if xpay_status in PAID_XPAY_STATUSES:
        if order_fee != expected_fen or paid_fee != expected_fen:
            return 'skipped'
        payload = dict(recharge.notify_payload or {})
        payload['query_order'] = order_data
        mark_recharge_paid(
            recharge,
            order_data.get('wx_order_id') or order_data.get('wxpay_order_id') or '',
            payload,
        )
        return 'credited'

    updated = RechargeOrder.objects.filter(
        pk=recharge.pk,
        status=RechargeOrder.STATUS_PAYING,
    ).update(status=RechargeOrder.STATUS_CLOSED, updated_at=timezone.now())
    return 'closed' if updated else 'skipped'


def coin_recharge_payload(recharge):
    stored = dict(recharge.notify_payload or {})
    scale = _recharge_coin_scale(recharge)
    payload = {
        'recharge_no': recharge.recharge_no,
        'payment_no': recharge.recharge_no,
        'status': recharge.status,
        'amount': str(qyuan(recharge.amount)),
        'pay_amount_yuan': str(qyuan(recharge.amount)),
        'diamonds': format_diamonds(recharge.amount),
        'diamonds_per_yuan': DIAMONDS_PER_YUAN,
        'wechat_coin_units': yuan_to_coin_units(recharge.amount, units_per_yuan=scale),
        'wechat_coin_units_per_yuan': scale,
        'recharge_product_id': recharge.product_id,
        'order_no': stored.get('checkout_order_no') or None,
        'checkout_order_no': stored.get('checkout_order_no') or None,
        'client_platform': stored.get('client_platform', 'other'),
        'platform_fee_percent': stored.get('platform_fee_percent'),
        'estimated_platform_fee_yuan': stored.get('estimated_platform_fee_yuan'),
        'estimated_settlement_yuan': stored.get('estimated_settlement_yuan'),
        'expires_at': timezone.localtime(recharge.expires_at).isoformat() if recharge.expires_at else None,
        'created_at': timezone.localtime(recharge.created_at).isoformat() if recharge.created_at else None,
        'paid_at': timezone.localtime(recharge.paid_at).isoformat() if recharge.paid_at else None,
        'credited_at': timezone.localtime(recharge.credited_at).isoformat() if recharge.credited_at else None,
    }
    if recharge.status == RechargeOrder.STATUS_CREDITED:
        wallet = getattr(recharge.profile, 'wallet', None)
        if wallet is not None:
            payload['balance_diamonds'] = format_diamonds(wallet.balance)
    return payload
