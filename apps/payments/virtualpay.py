import hashlib
import hmac
import json
from datetime import timedelta
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.orders.models import Order

from .models import Payment, VirtualProductBinding
from .services import amount_to_cents, ensure_order_owner, generate_payment_no, get_order_amount, mark_payment_paid


VIRTUAL_MODE_GOODS = 'short_series_goods'
VIRTUAL_CHANNEL = 'wechat_virtual'
VIRTUAL_PACKAGE_PREFIX = 'virtual_payment:'
PAID_XPAY_STATUSES = {2, 3, 4}


class VirtualPaymentError(Exception):
    pass


class VirtualPaymentConfigurationError(VirtualPaymentError):
    pass


class VirtualPaymentAPIError(VirtualPaymentError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message or f'微信虚拟支付接口错误：{code}')


def compact_json(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))


def hmac_sha256_hex(key, message):
    return hmac.new(key.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()


def virtual_env():
    env = int(settings.WECHAT_VIRTUALPAY_ENV)
    if env not in {0, 1}:
        raise VirtualPaymentConfigurationError('WECHAT_VIRTUALPAY_ENV 只能是0或1')
    return env


def validate_configuration():
    if not settings.WECHAT_VIRTUALPAY_ENABLED:
        raise VirtualPaymentConfigurationError('虚拟支付尚未启用')
    if not settings.WECHAT_APP_ID or not settings.WECHAT_APP_SECRET:
        raise VirtualPaymentConfigurationError('微信小程序 AppID/AppSecret 未配置')
    if not settings.WECHAT_VIRTUALPAY_OFFER_ID:
        raise VirtualPaymentConfigurationError('WECHAT_VIRTUALPAY_OFFER_ID 未配置')
    if not settings.WECHAT_VIRTUALPAY_APP_KEY:
        raise VirtualPaymentConfigurationError('WECHAT_VIRTUALPAY_APP_KEY 未配置')
    return virtual_env()


def exchange_code_for_session(code):
    if not code:
        raise ValidationError({'detail': '缺少 wx.login code，请重新进入支付页面'})
    query = urlencode({
        'appid': settings.WECHAT_APP_ID,
        'secret': settings.WECHAT_APP_SECRET,
        'js_code': code,
        'grant_type': 'authorization_code',
    })
    endpoint = f'https://api.weixin.qq.com/sns/jscode2session?{query}'
    try:
        with urlopen(endpoint, timeout=settings.WECHAT_VIRTUALPAY_HTTP_TIMEOUT) as response:
            data = json.loads(response.read().decode('utf-8'))
    except Exception as exc:
        raise VirtualPaymentAPIError('jscode2session_failed', '微信登录态换取失败，请稍后重试') from exc
    if not data.get('openid') or not data.get('session_key'):
        raise VirtualPaymentAPIError(data.get('errcode', -1), data.get('errmsg') or '微信登录态无效')
    return data['openid'], data['session_key']


def get_access_token():
    cache_key = f'wechat-access-token:{settings.WECHAT_APP_ID}'
    token = cache.get(cache_key)
    if token:
        return token
    query = urlencode({
        'grant_type': 'client_credential',
        'appid': settings.WECHAT_APP_ID,
        'secret': settings.WECHAT_APP_SECRET,
    })
    endpoint = f'https://api.weixin.qq.com/cgi-bin/token?{query}'
    try:
        with urlopen(endpoint, timeout=settings.WECHAT_VIRTUALPAY_HTTP_TIMEOUT) as response:
            data = json.loads(response.read().decode('utf-8'))
    except Exception as exc:
        raise VirtualPaymentAPIError('access_token_failed', '获取微信接口凭证失败') from exc
    token = data.get('access_token')
    if not token:
        raise VirtualPaymentAPIError(data.get('errcode', -1), data.get('errmsg') or '获取微信接口凭证失败')
    expires_in = max(60, int(data.get('expires_in') or 7200) - 300)
    cache.set(cache_key, token, expires_in)
    return token


def xpay_post(endpoint, payload):
    body = compact_json(payload)
    pay_sig = hmac_sha256_hex(settings.WECHAT_VIRTUALPAY_APP_KEY, f'{endpoint}&{body}')
    query = urlencode({'access_token': get_access_token(), 'pay_sig': pay_sig})
    request = Request(
        f'https://api.weixin.qq.com{endpoint}?{query}',
        data=body.encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=settings.WECHAT_VIRTUALPAY_HTTP_TIMEOUT) as response:
            data = json.loads(response.read().decode('utf-8'))
    except Exception as exc:
        raise VirtualPaymentAPIError('network_error', '微信虚拟支付接口暂不可用') from exc
    if int(data.get('errcode') or 0) != 0:
        raise VirtualPaymentAPIError(data.get('errcode'), data.get('errmsg') or '微信虚拟支付接口调用失败')
    return data


def resolve_virtual_product(order):
    items = list(order.items.select_related('package', 'spec').order_by('sort_order', 'id'))
    if len(items) != 1:
        raise ValidationError({'detail': '当前沙箱测试仅支持单个商品结算，请不要合并多个商品'})

    item = items[0]
    binding = None
    if item.spec_id:
        binding = VirtualProductBinding.objects.filter(spec_id=item.spec_id, is_active=True).first()
    if not binding:
        binding = VirtualProductBinding.objects.filter(package_id=item.package_id, spec__isnull=True, is_active=True).first()

    if binding:
        product_id = binding.product_id
        goods_price_fen = binding.goods_price_fen
        source = f'binding:{binding.id}'
    else:
        env = virtual_env()
        fallback_product_id = settings.WECHAT_VIRTUALPAY_SANDBOX_PRODUCT_ID.strip()
        fallback_price = int(settings.WECHAT_VIRTUALPAY_SANDBOX_PRICE_FEN)
        keyword = settings.WECHAT_VIRTUALPAY_SANDBOX_PACKAGE_KEYWORD.strip()
        item_unit_fen = amount_to_cents(item.unit_price)
        package_text = f'{item.package_name} {item.spec_name} {item.spec_display_name}'
        can_use_fallback = (
            env == 1
            and fallback_product_id
            and fallback_price > 0
            and item_unit_fen == fallback_price
            and (not keyword or keyword in package_text)
        )
        if not can_use_fallback:
            raise ValidationError({
                'detail': '该商品尚未绑定微信虚拟支付道具，请在后台“虚拟支付商品绑定”中配置'
            })
        product_id = fallback_product_id
        goods_price_fen = fallback_price
        source = 'sandbox_fallback'

    quantity = int(item.quantity or 1)
    expected_total_fen = goods_price_fen * quantity
    actual_total_fen = amount_to_cents(get_order_amount(order))
    if actual_total_fen != expected_total_fen:
        raise ValidationError({
            'detail': (
                f'订单金额¥{actual_total_fen / 100:.2f}与虚拟道具金额'
                f'¥{expected_total_fen / 100:.2f}不一致。沙箱测试请使用单个四套四弹15元商品，'
                '不要添加附加项、指定陪玩费用或动态计费。'
            )
        })

    return {
        'product_id': product_id,
        'goods_price_fen': goods_price_fen,
        'quantity': quantity,
        'expected_total_fen': expected_total_fen,
        'source': source,
        'item_id': item.id,
    }


@transaction.atomic
def create_virtual_payment(order_no, user, code):
    env = validate_configuration()
    order = (
        Order.objects
        .select_for_update()
        .select_related('boss_user')
        .prefetch_related('items__package', 'items__spec')
        .filter(order_no=order_no)
        .first()
    )
    if not order:
        raise ValidationError({'detail': '订单不存在'})
    ensure_order_owner(order, user)
    if order.paid or order.status == Order.STATUS_COMPLETED:
        raise ValidationError({'detail': '订单已支付'})
    if order.status != Order.STATUS_PENDING_PAYMENT:
        raise ValidationError({'detail': '当前订单状态不可支付'})

    profile = getattr(user, 'client_profile', None)
    if not profile or not profile.openid:
        raise PermissionDenied('当前账号未绑定微信 openid，请重新登录')
    openid, session_key = exchange_code_for_session(code)
    if openid != profile.openid:
        raise PermissionDenied('本次微信登录账号与订单账号不一致')

    product = resolve_virtual_product(order)
    Payment.objects.filter(
        order=order,
        channel=VIRTUAL_CHANNEL,
        status='paying',
    ).update(status='closed')

    payment_no = generate_payment_no()
    payment = Payment.objects.create(
        payment_no=payment_no,
        order=order,
        channel=VIRTUAL_CHANNEL,
        scene=VIRTUAL_MODE_GOODS,
        amount=float(get_order_amount(order)),
        status='paying',
        third_order_no=payment_no,
        expires_at=timezone.now() + timedelta(minutes=10),
        notify_payload={
            'virtual_payment': True,
            'env': env,
            **product,
        },
    )

    sign_data = compact_json({
        'offerId': str(settings.WECHAT_VIRTUALPAY_OFFER_ID),
        'buyQuantity': product['quantity'],
        'env': env,
        'currencyType': 'CNY',
        'productId': product['product_id'],
        'goodsPrice': product['goods_price_fen'],
        'outTradeNo': payment.payment_no,
        'attach': order.order_no,
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
        'mode': VIRTUAL_MODE_GOODS,
        'payment_no': payment.payment_no,
        'env': env,
    }
    encoded_payload = quote(compact_json(bridge_payload), safe='')
    return payment, {
        'signData': sign_data,
        'signature': signature,
        'mode': VIRTUAL_MODE_GOODS,
        # 兼容旧版支付页面保留的字段。
        'timeStamp': '0',
        'nonceStr': 'virtual-payment',
        'package': f'{VIRTUAL_PACKAGE_PREFIX}{encoded_payload}',
        'signType': 'VIRTUAL',
        'paySign': pay_sig,
        'payment_no': payment.payment_no,
        'order_no': order.order_no,
        'amount': payment.amount,
        'status': payment.status,
        'virtual': True,
        'virtual_env': env,
        'product_id': product['product_id'],
    }


def notify_goods_delivered(payment):
    payload = {
        'order_id': payment.payment_no,
        'env': virtual_env(),
    }
    return xpay_post('/xpay/notify_provide_goods', payload)


@transaction.atomic
def query_virtual_payment(payment_no, user):
    validate_configuration()
    payment = (
        Payment.objects
        .select_for_update()
        .select_related('order', 'order__boss_user')
        .filter(payment_no=payment_no)
        .first()
    )
    if not payment:
        raise ValidationError({'detail': '支付单不存在'})
    ensure_order_owner(payment.order, user)
    if payment.channel != VIRTUAL_CHANNEL:
        raise ValidationError({'detail': '该支付单不是微信虚拟支付'})
    if payment.status == 'paid':
        return payment

    profile = getattr(user, 'client_profile', None)
    if not profile or not profile.openid:
        raise PermissionDenied('当前账号未绑定微信 openid')

    response = xpay_post('/xpay/query_order', {
        'openid': profile.openid,
        'env': virtual_env(),
        'order_id': payment.payment_no,
    })
    order_data = response.get('order') or {}
    xpay_status = int(order_data.get('status') or 0)
    expected_fen = amount_to_cents(payment.amount)
    order_fee = int(order_data.get('order_fee') or 0)
    paid_fee = int(order_data.get('paid_fee') or 0)

    payload = dict(payment.notify_payload or {})
    payload['query_order'] = order_data
    payment.notify_payload = payload
    payment.third_trade_no = (
        order_data.get('wx_order_id')
        or order_data.get('wxpay_order_id')
        or payment.third_trade_no
    )
    payment.save(update_fields=['notify_payload', 'third_trade_no', 'updated_at'])

    if xpay_status in PAID_XPAY_STATUSES:
        if order_fee != expected_fen or paid_fee != expected_fen:
            raise VirtualPaymentError('微信虚拟支付订单金额校验失败，请联系管理员处理')
        payment = mark_payment_paid(
            payment,
            third_trade_no=payment.third_trade_no or '',
            payload=payload,
        )
        if xpay_status != 4:
            try:
                delivery_response = notify_goods_delivered(payment)
                payload = dict(payment.notify_payload or {})
                payload['delivery_response'] = delivery_response
                payment.notify_payload = payload
                payment.save(update_fields=['notify_payload', 'updated_at'])
            except VirtualPaymentError as exc:
                payload = dict(payment.notify_payload or {})
                payload['delivery_error'] = str(exc)
                payment.notify_payload = payload
                payment.save(update_fields=['notify_payload', 'updated_at'])
        return payment

    if xpay_status == 6:
        payment.status = 'closed'
        payment.save(update_fields=['status', 'updated_at'])
    elif xpay_status in {5, 7}:
        payment.status = 'failed'
        payment.save(update_fields=['status', 'updated_at'])
    return payment
