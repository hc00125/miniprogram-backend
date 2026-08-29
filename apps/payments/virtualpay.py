import hashlib
import hmac
import json
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.catalog.models import CompositionSku
from apps.orders.models import Order

from .models import Payment, VirtualProductBinding
from .services import amount_to_cents, ensure_order_owner, generate_payment_no, get_order_amount, mark_payment_paid


logger = logging.getLogger(__name__)

VIRTUAL_MODE_GOODS = 'short_series_goods'
VIRTUAL_CHANNEL = 'wechat_virtual'
VIRTUAL_PACKAGE_PREFIX = 'virtual_payment:'
PAID_XPAY_STATUSES = {2, 3, 4}
TOKEN_INVALID_CODES = {40001, 40014, 42001}


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


def _access_token_cache_key():
    return f'wechat-stable-access-token:{settings.WECHAT_APP_ID}'


def _fetch_stable_access_token(force_refresh=False):
    body = compact_json({
        'grant_type': 'client_credential',
        'appid': settings.WECHAT_APP_ID,
        'secret': settings.WECHAT_APP_SECRET,
        'force_refresh': bool(force_refresh),
    })
    request = Request(
        'https://api.weixin.qq.com/cgi-bin/stable_token',
        data=body.encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=settings.WECHAT_VIRTUALPAY_HTTP_TIMEOUT) as response:
            data = json.loads(response.read().decode('utf-8'))
    except Exception as exc:
        raise VirtualPaymentAPIError('access_token_failed', '获取微信稳定版接口凭证失败') from exc

    token = data.get('access_token')
    if not token:
        raise VirtualPaymentAPIError(data.get('errcode', -1), data.get('errmsg') or '获取微信稳定版接口凭证失败')
    return token, int(data.get('expires_in') or 7200)


def get_access_token(force_refresh=False):
    cache_key = _access_token_cache_key()
    if not force_refresh:
        token = cache.get(cache_key)
        if token:
            return token

    token, expires_in = _fetch_stable_access_token(force_refresh=force_refresh)
    cache.set(cache_key, token, max(60, expires_in - 300))
    return token


def _xpay_post_once(endpoint, body, pay_sig, access_token):
    query = urlencode({'access_token': access_token, 'pay_sig': pay_sig})
    request = Request(
        f'https://api.weixin.qq.com{endpoint}?{query}',
        data=body.encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=settings.WECHAT_VIRTUALPAY_HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as exc:
        raise VirtualPaymentAPIError('network_error', '微信虚拟支付接口暂不可用') from exc


def xpay_post(endpoint, payload):
    body = compact_json(payload)
    pay_sig = hmac_sha256_hex(settings.WECHAT_VIRTUALPAY_APP_KEY, f'{endpoint}&{body}')

    data = _xpay_post_once(endpoint, body, pay_sig, get_access_token())
    errcode = int(data.get('errcode') or 0)

    if errcode in TOKEN_INVALID_CODES:
        logger.warning(
            '[虚拟支付] access_token失效，刷新后重试 endpoint=%s order_id=%s errcode=%s',
            endpoint,
            payload.get('order_id', ''),
            errcode,
        )
        cache.delete(_access_token_cache_key())
        data = _xpay_post_once(endpoint, body, pay_sig, get_access_token(force_refresh=True))
        errcode = int(data.get('errcode') or 0)

    if errcode != 0:
        logger.error(
            '[虚拟支付] 微信接口失败 endpoint=%s order_id=%s errcode=%s errmsg=%s',
            endpoint,
            payload.get('order_id', ''),
            data.get('errcode'),
            data.get('errmsg'),
        )
        raise VirtualPaymentAPIError(data.get('errcode'), data.get('errmsg') or '微信虚拟支付接口调用失败')
    return data


def resolve_virtual_product(order):
    if order.pricing_mode == Order.PRICING_MODE_COMPOSITION:
        return resolve_composition_virtual_product(order)

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


def _composition_duration(order):
    try:
        raw_duration = Decimal(str(order.booked_hours))
        duration = int(raw_duration)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({'detail': '静态组合订单的时长必须是正整数小时'}) from exc
    if duration < 1 or raw_duration != Decimal(duration):
        raise ValidationError({'detail': '静态组合订单的时长必须是正整数小时'})
    return duration


def resolve_composition_virtual_product(order):
    """Map a composition-priced order to its one static virtual product.

    The concrete designated players never participate in this lookup.  The
    order must still point at the active catalog CompositionSku with the exact
    key, internal virtual spec and per-hour price snapshot it was quoted with.
    """
    if order.composition_pricing_error:
        # A decline/timeout is allowed to free its seat even when operations
        # forgot the fallback SKU.  If they repair that configuration later,
        # lazily re-match it here; otherwise payment remains blocked with the
        # recorded configuration error and never falls back to legacy pricing.
        from apps.orders.composition_pricing import reprice_composition_order

        previous_error = order.composition_pricing_error
        try:
            reprice_composition_order(order, require_virtual_binding=True)
        except ValidationError:
            raise ValidationError({
                'detail': f'当前指定组合缺少可支付的静态 SKU 配置：{previous_error}',
            })
    if not order.composition_sku_id:
        raise ValidationError({'detail': '静态组合订单缺少组合 SKU 快照，不能发起支付'})
    sku = (
        CompositionSku.objects.select_related('virtual_package_spec')
        .filter(pk=order.composition_sku_id, is_active=True)
        .first()
    )
    if not sku:
        raise ValidationError({'detail': '当前组合 SKU 已下架或不存在，不能发起支付'})
    if not sku.virtual_package_spec_id:
        raise ValidationError({'detail': '当前组合 SKU 未配置内部虚拟商品规格'})
    if order.composition_key != sku.composition_key:
        raise ValidationError({'detail': '订单组合 SKU 键与当前静态配置不一致，不能支付'})
    if order.composition_virtual_spec_id != sku.virtual_package_spec_id:
        raise ValidationError({'detail': '订单虚拟商品规格与当前组合 SKU 不一致，不能支付'})

    per_hour_fen = amount_to_cents(sku.total_price_per_hour)
    snapshot_fen = amount_to_cents(order.composition_price_per_hour)
    if per_hour_fen <= 0 or snapshot_fen != per_hour_fen:
        raise ValidationError({'detail': '订单组合价格快照与静态 SKU 不一致，不能支付'})
    if amount_to_cents(sku.virtual_package_spec.price) != per_hour_fen:
        raise ValidationError({'detail': '组合 SKU 的内部虚拟规格价格不正确，不能支付'})

    binding = sku.active_virtual_binding()
    if not binding:
        raise ValidationError({'detail': '当前组合 SKU 未绑定价格一致的微信虚拟商品，不能支付'})
    if binding.spec_id != sku.virtual_package_spec_id or binding.goods_price_fen != per_hour_fen:
        raise ValidationError({'detail': '组合 SKU 微信虚拟商品绑定不一致，不能支付'})

    quantity = _composition_duration(order)
    expected_total_fen = binding.goods_price_fen * quantity
    actual_total_fen = amount_to_cents(get_order_amount(order))
    if actual_total_fen != expected_total_fen:
        raise ValidationError({'detail': '静态组合订单总金额与“组合 SKU 单价 × 时长”不一致，不能支付'})

    return {
        'product_id': binding.product_id,
        'goods_price_fen': binding.goods_price_fen,
        'quantity': quantity,
        'expected_total_fen': expected_total_fen,
        'source': f'composition_sku:{sku.id}',
        'item_id': None,
        'composition_sku_id': sku.id,
        'composition_key': sku.composition_key,
        'virtual_package_spec_id': sku.virtual_package_spec_id,
    }


def _product_from_payment(payment, fallback=None):
    stored = dict(payment.notify_payload or {})
    fallback = fallback or {}
    product = {
        'product_id': stored.get('product_id') or fallback.get('product_id'),
        'goods_price_fen': int(stored.get('goods_price_fen') or fallback.get('goods_price_fen') or 0),
        'quantity': int(stored.get('quantity') or fallback.get('quantity') or 1),
        'expected_total_fen': int(stored.get('expected_total_fen') or fallback.get('expected_total_fen') or 0),
        'source': stored.get('source') or fallback.get('source') or 'existing_payment',
        'item_id': stored.get('item_id') or fallback.get('item_id'),
    }
    if not product['product_id'] or product['goods_price_fen'] <= 0:
        raise VirtualPaymentError('已有支付单缺少微信虚拟道具信息，请联系管理员处理')
    if stored.get('composition_sku_id'):
        product['composition_sku_id'] = stored.get('composition_sku_id')
        product['composition_key'] = stored.get('composition_key', '')
        product['virtual_package_spec_id'] = stored.get('virtual_package_spec_id')
    return product


def _build_virtual_payment_payload(payment, order, product, env, session_key):
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
    return {
        'signData': sign_data,
        'paySig': pay_sig,
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


def create_virtual_payment(order_no, user, code):
    env = validate_configuration()

    profile = getattr(user, 'client_profile', None)
    if not profile or not profile.openid:
        raise PermissionDenied('当前账号未绑定微信 openid，请重新登录')
    openid, session_key = exchange_code_for_session(code)
    if openid != profile.openid:
        raise PermissionDenied('本次微信登录账号与订单账号不一致')

    now = timezone.now()
    existing = (
        Payment.objects
        .select_related('order')
        .filter(
            order__order_no=order_no,
            channel=VIRTUAL_CHANNEL,
            scene=VIRTUAL_MODE_GOODS,
            status='paying',
        )
        .order_by('-created_at')
        .first()
    )
    if existing:
        ensure_order_owner(existing.order, user)
        if not existing.expires_at or existing.expires_at > now:
            if existing.order.pricing_mode == Order.PRICING_MODE_COMPOSITION:
                # Do not reuse a stale virtual order after the static SKU has
                # been disabled or its product binding no longer matches.
                current = resolve_virtual_product(existing.order)
                product = _product_from_payment(existing, current)
                for field in ('product_id', 'goods_price_fen', 'quantity', 'expected_total_fen'):
                    if product[field] != current[field]:
                        raise ValidationError({'detail': '已有支付单与当前静态组合 SKU 不一致，请重新创建支付单'})
            else:
                product = _product_from_payment(existing)
            return existing, _build_virtual_payment_payload(
                existing,
                existing.order,
                product,
                env,
                session_key,
            )

        # 旧支付单已过本地有效期，但微信侧结果仍可能是“已支付”。
        # 只有在主动查单明确未支付后，才允许关闭旧单并创建新单。
        synced = query_virtual_payment(existing.payment_no, user)
        if synced.status == 'paid' or getattr(synced.order, 'paid', False):
            raise ValidationError({'detail': '订单已支付，请刷新订单状态'})

    with transaction.atomic():
        order = (
            Order.objects
            .select_for_update(of=('self',))
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

        product = resolve_virtual_product(order)
        active = (
            Payment.objects
            .select_for_update()
            .filter(
                order=order,
                channel=VIRTUAL_CHANNEL,
                scene=VIRTUAL_MODE_GOODS,
                status='paying',
                expires_at__gt=now,
            )
            .order_by('-created_at')
            .first()
        )
        if active:
            stored_product = _product_from_payment(active, product)
            if order.pricing_mode == Order.PRICING_MODE_COMPOSITION:
                for field in ('product_id', 'goods_price_fen', 'quantity', 'expected_total_fen'):
                    if stored_product[field] != product[field]:
                        raise ValidationError({'detail': '已有支付单与当前静态组合 SKU 不一致，请重新创建支付单'})
            product = stored_product
            return active, _build_virtual_payment_payload(active, order, product, env, session_key)

        Payment.objects.filter(
            order=order,
            channel=VIRTUAL_CHANNEL,
            scene=VIRTUAL_MODE_GOODS,
            status='paying',
        ).update(status='closed', updated_at=timezone.now())

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
                'delivery_status': 'pending',
                **product,
            },
        )

    return payment, _build_virtual_payment_payload(payment, order, product, env, session_key)


def notify_goods_delivered(payment):
    payload = {
        'order_id': payment.payment_no,
        'env': virtual_env(),
    }
    return xpay_post('/xpay/notify_provide_goods', payload)


def _delivery_succeeded(payment):
    payload = dict(payment.notify_payload or {})
    response = payload.get('delivery_response') or {}
    return (
        payload.get('delivery_status') == 'succeeded'
        or (isinstance(response, dict) and bool(response) and int(response.get('errcode') or 0) == 0)
    )


def _save_delivery_result(payment, response=None, error=None):
    with transaction.atomic():
        locked = Payment.objects.select_for_update().get(pk=payment.pk)
        payload = dict(locked.notify_payload or {})
        attempts = int(payload.get('delivery_attempts') or 0) + 1
        payload['delivery_attempts'] = attempts
        payload['delivery_last_attempt_at'] = timezone.now().isoformat()

        if error is None:
            payload['delivery_status'] = 'succeeded'
            payload['delivery_response'] = response or {'errcode': 0, 'errmsg': 'OK'}
            payload.pop('delivery_error', None)
        else:
            payload['delivery_status'] = 'failed'
            payload['delivery_error'] = {
                'code': getattr(error, 'code', ''),
                'message': str(error)[:500],
            }

        locked.notify_payload = payload
        locked.updated_at = timezone.now()
        locked.save(update_fields=['notify_payload', 'updated_at'])
        return locked


def _deliver_paid_payment(payment):
    if _delivery_succeeded(payment):
        return payment

    try:
        response = notify_goods_delivered(payment)
    except Exception as exc:
        logger.exception(
            '[虚拟支付] 支付已确认但发货通知失败 payment_no=%s error=%s',
            payment.payment_no,
            exc,
        )
        # 发货失败不能回滚或否定已完成的支付；记录失败，后续刷新/对账时重试。
        return _save_delivery_result(payment, error=exc)

    return _save_delivery_result(payment, response=response)


def query_virtual_payment(payment_no, user):
    validate_configuration()
    payment = (
        Payment.objects
        .select_related('order', 'order__boss_user')
        .filter(payment_no=payment_no)
        .first()
    )
    if not payment:
        raise ValidationError({'detail': '支付单不存在'})
    ensure_order_owner(payment.order, user)
    if payment.channel != VIRTUAL_CHANNEL:
        raise ValidationError({'detail': '该支付单不是微信虚拟支付'})

    # 本地已确认付款时，不再查询或创建新支付单；仅补偿性重试发货。
    if payment.status == 'paid':
        return _deliver_paid_payment(payment)

    profile = getattr(user, 'client_profile', None)
    if not profile or not profile.openid:
        raise PermissionDenied('当前账号未绑定微信 openid')

    # 微信网络请求必须在数据库事务之外执行，避免发货或凭证错误回滚已支付状态。
    response = xpay_post('/xpay/query_order', {
        'openid': profile.openid,
        'env': virtual_env(),
        'order_id': payment.payment_no,
    })
    order_data = response.get('order') or {}
    xpay_status = int(order_data.get('status') or 0)
    order_fee = int(order_data.get('order_fee') or 0)
    paid_fee = int(order_data.get('paid_fee') or 0)

    with transaction.atomic():
        # 统一锁序：先锁 order 再锁 payment（与 mark_payment_paid、
        # wallet.pay_order_with_balance 一致），避免跨事务循环等待。
        # Payment.order 是 to_field='order_no' 外键，order_id 即订单号字符串。
        Order.objects.select_for_update().get(order_no=payment.order_id)
        payment = (
            Payment.objects
            .select_for_update(of=('self',))
            .select_related('order', 'order__boss_user')
            .get(pk=payment.pk)
        )
        ensure_order_owner(payment.order, user)
        expected_fen = amount_to_cents(payment.amount)

        payload = dict(payment.notify_payload or {})
        payload['query_order'] = order_data
        payment.notify_payload = payload
        payment.third_trade_no = (
            order_data.get('wx_order_id')
            or order_data.get('wxpay_order_id')
            or payment.third_trade_no
        )
        payment.updated_at = timezone.now()
        payment.save(update_fields=['notify_payload', 'third_trade_no', 'updated_at'])

        if xpay_status in PAID_XPAY_STATUSES:
            if order_fee != expected_fen or paid_fee != expected_fen:
                raise VirtualPaymentError('微信虚拟支付订单金额校验失败，请联系管理员处理')
            payment = mark_payment_paid(
                payment,
                payment.third_trade_no or payment.payment_no,
                payload,
            )

    # 到这里数据库中的 paid/order.paid 已经提交。发货失败只记录并等待重试。
    # 仅当本单确实被标记为已支付时才 ack 发货：迟到捕获（订单已由其他支付单
    # 支付，mark_payment_paid 拒绝重复标记）不发货，微信会对未发货的虚拟支付
    # 订单自动退款，这是对重复扣款的正确补偿。
    if xpay_status in PAID_XPAY_STATUSES and payment.status == 'paid':
        return _deliver_paid_payment(payment)
    return payment
