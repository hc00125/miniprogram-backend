import logging
import uuid
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.orders.models import Order
from apps.orders.payment_deadlines import ensure_payment_window_open
from apps.payments.models import Payment, Refund
from apps.payments.services import (
    amount_to_cents,
    ensure_order_owner,
    generate_payment_no,
    get_order_amount,
    mark_payment_paid,
)
from apps.payments.virtualpay import (
    PAID_XPAY_STATUSES,
    VIRTUAL_MODE_GOODS,
    VIRTUAL_CHANNEL,
    VIRTUAL_PACKAGE_PREFIX,
    VirtualPaymentError,
    compact_json,
    exchange_code_for_session,
    hmac_sha256_hex,
    query_virtual_payment,
    validate_configuration,
    virtual_env,
    xpay_post,
)

from .models import ClientWallet, ClientWalletLedger, RechargeOrder, RechargeProduct


logger = logging.getLogger(__name__)

CENT = Decimal('0.01')
ZERO = Decimal('0.00')

BALANCE_CHANNEL = 'balance'
BALANCE_SCENE = 'balance'
RECHARGE_ATTACH = 'recharge'
RECHARGE_EXPIRE_MINUTES = 10


def qmoney(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def generate_recharge_no():
    return f"RCG{timezone.localtime().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:8].upper()}"


def get_or_lock_wallet(profile):
    wallet, _created = ClientWallet.objects.get_or_create(profile=profile)
    return ClientWallet.objects.select_for_update().get(pk=wallet.pk)


def _existing_ledger_entry(wallet, entry_type, reference_id):
    if not reference_id:
        return None
    return ClientWalletLedger.objects.filter(
        wallet=wallet,
        entry_type=entry_type,
        reference_id=reference_id,
    ).first()


def write_wallet_ledger(
    wallet,
    entry_type,
    amount,
    reference_type='',
    reference_id='',
    note='',
    operator=None,
):
    """钱包余额变动唯一入口：更新余额并追加一条流水，入账恰好一次。

    调用方必须已经在事务中通过 ``get_or_lock_wallet`` 锁定钱包行。带
    ``reference_id`` 的写入靠账本唯一约束防重：撞 ``IntegrityError`` 时视为
    已入账，返回已存在的流水并且不再变更余额。返回 ``(entry, created)``。
    """
    amount = qmoney(amount)
    if amount == ZERO:
        raise ValidationError({'detail': '钱包变动金额不能为0'})
    reference_id = str(reference_id or '')

    existing = _existing_ledger_entry(wallet, entry_type, reference_id)
    if existing:
        return existing, False

    balance_after = qmoney(qmoney(wallet.balance) + amount)
    if balance_after < ZERO:
        raise ValidationError({'detail': '余额不足'})

    try:
        with transaction.atomic():
            entry = ClientWalletLedger.objects.create(
                wallet=wallet,
                entry_type=entry_type,
                amount=amount,
                balance_after=balance_after,
                reference_type=reference_type or '',
                reference_id=reference_id,
                note=(note or '')[:500],
                operator=operator if getattr(operator, 'is_authenticated', False) else None,
            )
    except IntegrityError:
        # 唯一约束兜底：并发重复入账时回滚本次流水写入，余额保持不变。
        existing = _existing_ledger_entry(wallet, entry_type, reference_id)
        if existing:
            return existing, False
        raise

    wallet.balance = balance_after
    wallet.save(update_fields=['balance', 'updated_at'])
    return entry, True


def ensure_recharge_owner(recharge, user):
    if not user or not getattr(user, 'is_authenticated', False):
        raise PermissionDenied('请先登录')
    if recharge.profile.user_id != user.id:
        raise PermissionDenied('无权操作该充值单')


def _product_from_recharge(recharge, fallback=None):
    stored = dict(recharge.notify_payload or {})
    fallback = fallback or {}
    product = {
        'product_id': stored.get('product_id') or fallback.get('product_id'),
        'goods_price_fen': int(stored.get('goods_price_fen') or fallback.get('goods_price_fen') or 0),
    }
    if not product['product_id'] or product['goods_price_fen'] <= 0:
        raise VirtualPaymentError('已有充值单缺少微信虚拟道具信息，请联系管理员处理')
    return product


def _build_recharge_payment_payload(recharge, product_info, env, session_key):
    sign_data = compact_json({
        'offerId': str(settings.WECHAT_VIRTUALPAY_OFFER_ID),
        'buyQuantity': 1,
        'env': env,
        'currencyType': 'CNY',
        'productId': product_info['product_id'],
        'goodsPrice': product_info['goods_price_fen'],
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
        'mode': VIRTUAL_MODE_GOODS,
        'payment_no': recharge.recharge_no,
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
        'payment_no': recharge.recharge_no,
        'recharge_no': recharge.recharge_no,
        'amount': str(qmoney(recharge.amount)),
        'status': recharge.status,
        'virtual': True,
        'virtual_env': env,
        'product_id': product_info['product_id'],
    }


def build_mock_recharge_request(recharge):
    return {
        'timeStamp': str(int(timezone.now().timestamp())),
        'nonceStr': uuid.uuid4().hex,
        'package': f'prepay_id=mock_{recharge.recharge_no}',
        'signType': 'RSA',
        'paySign': uuid.uuid4().hex,
        'payment_no': recharge.recharge_no,
        'recharge_no': recharge.recharge_no,
        'amount': str(qmoney(recharge.amount)),
        'status': recharge.status,
        'mock': True,
    }


def _get_active_recharge_product(product_pk):
    product = RechargeProduct.objects.filter(pk=product_pk, is_active=True).first()
    if not product:
        raise ValidationError({'detail': '充值档位不存在或已下架'})
    if product.goods_price_fen != amount_to_cents(product.amount):
        raise ValidationError({'detail': '充值档位价格配置不一致，请联系管理员'})
    return product


def _create_mock_recharge(profile, product):
    """本地联调专用：不触达微信，直接创建 mock 通道充值单。"""
    now = timezone.now()
    existing = (
        RechargeOrder.objects
        .filter(
            profile=profile,
            product=product,
            channel=RechargeOrder.CHANNEL_MOCK,
            status=RechargeOrder.STATUS_PAYING,
            expires_at__gt=now,
        )
        .order_by('-created_at')
        .first()
    )
    if existing:
        return existing, build_mock_recharge_request(existing)

    RechargeOrder.objects.filter(
        profile=profile,
        product=product,
        channel=RechargeOrder.CHANNEL_MOCK,
        status=RechargeOrder.STATUS_PAYING,
    ).update(status=RechargeOrder.STATUS_CLOSED, updated_at=now)

    recharge = RechargeOrder.objects.create(
        recharge_no=generate_recharge_no(),
        profile=profile,
        product=product,
        amount=qmoney(product.amount),
        channel=RechargeOrder.CHANNEL_MOCK,
        status=RechargeOrder.STATUS_PAYING,
        expires_at=now + timedelta(minutes=RECHARGE_EXPIRE_MINUTES),
        notify_payload={
            'recharge': True,
            'mock': True,
            'product_id': product.product_id,
            'goods_price_fen': product.goods_price_fen,
        },
    )
    return recharge, build_mock_recharge_request(recharge)


def create_recharge(user, product_pk, code):
    profile = getattr(user, 'client_profile', None)
    if not profile or not profile.openid:
        raise PermissionDenied('当前账号未绑定微信 openid，请重新登录')

    if settings.ENABLE_MOCK_PAYMENT and not settings.WECHAT_VIRTUALPAY_ENABLED:
        product = _get_active_recharge_product(product_pk)
        return _create_mock_recharge(profile, product)

    env = validate_configuration()
    openid, session_key = exchange_code_for_session(code)
    if openid != profile.openid:
        raise PermissionDenied('本次微信登录账号与当前账号不一致')

    product = _get_active_recharge_product(product_pk)
    product_info = {
        'product_id': product.product_id,
        'goods_price_fen': product.goods_price_fen,
    }

    now = timezone.now()
    existing = (
        RechargeOrder.objects
        .select_related('profile')
        .filter(
            profile=profile,
            product=product,
            channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
            status=RechargeOrder.STATUS_PAYING,
        )
        .order_by('-created_at')
        .first()
    )
    if existing:
        if not existing.expires_at or existing.expires_at > now:
            stored = _product_from_recharge(existing, product_info)
            return existing, _build_recharge_payment_payload(existing, stored, env, session_key)

        # 旧充值单已过本地有效期，但微信侧结果仍可能是“已支付”。
        # 只有在主动查单明确未支付后，才允许关闭旧单并创建新单。
        synced = query_recharge(existing.recharge_no, user)
        if synced.status in {RechargeOrder.STATUS_PAID, RechargeOrder.STATUS_CREDITED}:
            raise ValidationError({'detail': '该充值单已支付，余额已入账，请勿重复充值'})
        RechargeOrder.objects.filter(pk=existing.pk, status=RechargeOrder.STATUS_PAYING).update(
            status=RechargeOrder.STATUS_CLOSED,
            updated_at=timezone.now(),
        )

    with transaction.atomic():
        recharge = RechargeOrder.objects.create(
            recharge_no=generate_recharge_no(),
            profile=profile,
            product=product,
            amount=qmoney(product.amount),
            channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
            status=RechargeOrder.STATUS_PAYING,
            expires_at=now + timedelta(minutes=RECHARGE_EXPIRE_MINUTES),
            notify_payload={
                'recharge': True,
                'env': env,
                'delivery_status': 'pending',
                **product_info,
            },
        )

    return recharge, _build_recharge_payment_payload(recharge, product_info, env, session_key)


def notify_recharge_delivered(recharge):
    return xpay_post('/xpay/notify_provide_goods', {
        'order_id': recharge.recharge_no,
        'env': virtual_env(),
    })


def _recharge_delivery_succeeded(recharge):
    payload = dict(recharge.notify_payload or {})
    response = payload.get('delivery_response') or {}
    return (
        payload.get('delivery_status') == 'succeeded'
        or (isinstance(response, dict) and bool(response) and int(response.get('errcode') or 0) == 0)
    )


def _save_recharge_delivery_result(recharge, response=None, error=None):
    with transaction.atomic():
        locked = RechargeOrder.objects.select_for_update().get(pk=recharge.pk)
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
        locked.save(update_fields=['notify_payload', 'updated_at'])
        return locked


def _deliver_paid_recharge(recharge):
    if recharge.channel != RechargeOrder.CHANNEL_WECHAT_VIRTUAL:
        return recharge
    if _recharge_delivery_succeeded(recharge):
        return recharge

    try:
        response = notify_recharge_delivered(recharge)
    except Exception as exc:
        logger.exception(
            '[钱包充值] 入账已确认但发货通知失败 recharge_no=%s error=%s',
            recharge.recharge_no,
            exc,
        )
        # 发货失败不能回滚或否定已完成的入账；记录失败，后续刷新/对账时重试。
        return _save_recharge_delivery_result(recharge, error=exc)

    return _save_recharge_delivery_result(recharge, response=response)


@transaction.atomic
def mark_recharge_paid(recharge, third_trade_no='', payload=None):
    recharge = (
        RechargeOrder.objects
        .select_for_update()
        .select_related('profile')
        .get(pk=recharge.pk)
    )
    if recharge.credited_at:
        return recharge

    paid_at = recharge.paid_at or timezone.now()
    recharge.status = RechargeOrder.STATUS_PAID
    recharge.third_trade_no = third_trade_no or recharge.third_trade_no
    if payload is not None:
        recharge.notify_payload = payload
    recharge.paid_at = paid_at
    recharge.save(update_fields=[
        'status',
        'third_trade_no',
        'notify_payload',
        'paid_at',
        'updated_at',
    ])
    return credit_recharge(recharge)


def credit_recharge(recharge):
    """余额入账恰好一次。调用方必须已在事务中持有充值单行锁。"""
    if recharge.credited_at:
        return recharge

    wallet = get_or_lock_wallet(recharge.profile)
    amount = qmoney(recharge.amount)
    _entry, created = write_wallet_ledger(
        wallet,
        ClientWalletLedger.TYPE_RECHARGE,
        amount,
        reference_type='recharge_order',
        reference_id=recharge.recharge_no,
        note=f'充值单 {recharge.recharge_no} 支付成功入账',
    )
    if created:
        wallet.recharged_total = qmoney(wallet.recharged_total + amount)
        wallet.save(update_fields=['recharged_total', 'updated_at'])

    recharge.credited_at = timezone.now()
    recharge.status = RechargeOrder.STATUS_CREDITED
    recharge.save(update_fields=['credited_at', 'status', 'updated_at'])
    return recharge


def query_recharge(recharge_no, user):
    if not user or not getattr(user, 'is_authenticated', False):
        raise PermissionDenied('请先登录')
    # 统一按归属过滤：单号缺失与非本人一律返回“充值单不存在”，
    # 避免通过 400/403 差异探测他人充值单号的存在性。
    recharge = (
        RechargeOrder.objects
        .select_related('profile', 'profile__user')
        .filter(recharge_no=recharge_no, profile__user=user)
        .first()
    )
    if not recharge:
        raise ValidationError({'detail': '充值单不存在'})

    # 本地已入账时，不再查询微信；仅补偿性重试发货。
    if recharge.status == RechargeOrder.STATUS_CREDITED:
        return _deliver_paid_recharge(recharge)

    if recharge.channel == RechargeOrder.CHANNEL_MOCK:
        return recharge

    validate_configuration()
    profile = recharge.profile
    if not profile.openid:
        raise PermissionDenied('当前账号未绑定微信 openid')

    # 微信网络请求必须在数据库事务之外执行，避免发货或凭证错误回滚已入账状态。
    response = xpay_post('/xpay/query_order', {
        'openid': profile.openid,
        'env': virtual_env(),
        'order_id': recharge.recharge_no,
    })
    order_data = response.get('order') or {}
    xpay_status = int(order_data.get('status') or 0)
    order_fee = int(order_data.get('order_fee') or 0)
    paid_fee = int(order_data.get('paid_fee') or 0)

    with transaction.atomic():
        recharge = (
            RechargeOrder.objects
            .select_for_update(of=('self',))
            .select_related('profile')
            .get(pk=recharge.pk)
        )
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

    # 到这里数据库中的入账已经提交。发货失败只记录并等待重试。
    if xpay_status in PAID_XPAY_STATUSES:
        return _deliver_paid_recharge(recharge)
    return recharge


def recharge_status_payload(recharge):
    payload = {
        'recharge_no': recharge.recharge_no,
        'status': recharge.status,
        'amount': str(qmoney(recharge.amount)),
    }
    if recharge.status == RechargeOrder.STATUS_CREDITED:
        wallet = ClientWallet.objects.filter(profile_id=recharge.profile_id).first()
        payload['balance'] = str(qmoney(wallet.balance if wallet else 0))
    return payload


def reconcile_recharge_order(recharge):
    """对过期仍处于支付中的充值单远程查单对账。

    已支付 → 入账并补发货；明确未支付 → 关闭；远程状态不明（异常）→ 跳过
    留待下轮，绝不盲关。
    """
    try:
        response = xpay_post('/xpay/query_order', {
            'openid': recharge.profile.openid,
            'env': virtual_env(),
            'order_id': recharge.recharge_no,
        })
    except Exception:
        logger.exception('[钱包充值] 对账查单失败，暂缓处理 recharge_no=%s', recharge.recharge_no)
        return 'skipped'

    order_data = response.get('order') or {}
    xpay_status = int(order_data.get('status') or 0)
    order_fee = int(order_data.get('order_fee') or 0)
    paid_fee = int(order_data.get('paid_fee') or 0)
    expected_fen = amount_to_cents(recharge.amount)

    if xpay_status in PAID_XPAY_STATUSES:
        if order_fee != expected_fen or paid_fee != expected_fen:
            logger.error(
                '[钱包充值] 对账金额不一致，拒绝入账 recharge_no=%s order_fee=%s paid_fee=%s expected=%s',
                recharge.recharge_no,
                order_fee,
                paid_fee,
                expected_fen,
            )
            return 'skipped'
        payload = dict(recharge.notify_payload or {})
        payload['query_order'] = order_data
        recharge = mark_recharge_paid(
            recharge,
            order_data.get('wx_order_id') or order_data.get('wxpay_order_id') or '',
            payload,
        )
        _deliver_paid_recharge(recharge)
        return 'credited'

    updated = RechargeOrder.objects.filter(
        pk=recharge.pk,
        status=RechargeOrder.STATUS_PAYING,
    ).update(status=RechargeOrder.STATUS_CLOSED, updated_at=timezone.now())
    return 'closed' if updated else 'skipped'


def pay_order_with_balance(order_no, user):
    order = ensure_payment_window_open(order_no, user)
    ensure_order_owner(order, user)

    profile = getattr(user, 'client_profile', None)
    if not profile:
        raise ValidationError({'detail': '请先微信登录'})

    now = timezone.now()
    virtual_paying = list(
        Payment.objects
        .filter(
            order__order_no=order_no,
            channel=VIRTUAL_CHANNEL,
            scene=VIRTUAL_MODE_GOODS,
            status='paying',
        )
        .order_by('-created_at')
    )
    verified_unpaid_pks = set()
    for payment in virtual_paying:
        if not payment.expires_at or payment.expires_at > now:
            raise ValidationError({'detail': '存在进行中的微信支付，请完成或等待其过期后再用余额支付'})
        # 旧微信支付单已过本地有效期，但微信侧结果仍可能是“已支付”。
        # 只有在主动查单明确未支付后，才允许关闭旧单并改用余额支付。
        synced = query_virtual_payment(payment.payment_no, user)
        if synced.status == 'paid' or getattr(synced.order, 'paid', False):
            raise ValidationError({'detail': '订单已通过微信支付，请刷新订单状态'})
        verified_unpaid_pks.add(payment.pk)

    with transaction.atomic():
        order = (
            Order.objects
            .select_for_update(of=('self',))
            .select_related('boss_user')
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

        amount = get_order_amount(order)
        if amount <= ZERO:
            raise ValidationError({'detail': '订单金额不正确'})

        # 事务内重查该订单所有通道的进行中支付单，闭合预检与加锁之间的竞态窗口
        # （TOCTOU）：只允许关闭“预检时已远程确认未支付”的过期虚拟支付单；出现
        # 任何预检之外的进行中支付（并发新建的虚拟单、jsapi/mock 单）都拒绝。
        # create_virtual_payment 新建支付单前必先锁 order，本事务已持有 order
        # 锁，因此这里看到的就是完整集合。
        paying_now = list(
            Payment.objects
            .select_for_update()
            .filter(order=order, status='paying')
        )
        for payment in paying_now:
            unverified = payment.pk not in verified_unpaid_pks
            not_virtual = payment.channel != VIRTUAL_CHANNEL or payment.scene != VIRTUAL_MODE_GOODS
            unexpired = not payment.expires_at or payment.expires_at > timezone.now()
            if unverified or not_virtual or unexpired:
                raise ValidationError({'detail': '存在进行中的微信支付，请完成或等待其过期后再用余额支付'})
        if paying_now:
            Payment.objects.filter(
                pk__in=[payment.pk for payment in paying_now],
                status='paying',
            ).update(status='closed', updated_at=timezone.now())

        wallet = get_or_lock_wallet(profile)
        if qmoney(wallet.balance) < amount:
            raise ValidationError({'detail': '余额不足'})

        payment_no = generate_payment_no()
        write_wallet_ledger(
            wallet,
            ClientWalletLedger.TYPE_ORDER_PAYMENT,
            -amount,
            reference_type='payment',
            reference_id=payment_no,
            note=f'余额支付订单 {order.order_no}',
            operator=user,
        )
        wallet.spent_total = qmoney(wallet.spent_total + amount)
        wallet.save(update_fields=['spent_total', 'updated_at'])

        payment = Payment.objects.create(
            payment_no=payment_no,
            order=order,
            channel=BALANCE_CHANNEL,
            scene=BALANCE_SCENE,
            amount=float(amount),
            status='paying',
            third_order_no=payment_no,
            notify_payload={'balance_payment': True},
        )
        payment = mark_payment_paid(payment, payment_no, {'balance_payment': True})

    return {
        'payment_no': payment.payment_no,
        'order_no': order.order_no,
        'status': payment.status,
        'amount': str(qmoney(amount)),
        'balance': str(qmoney(wallet.balance)),
    }


def refund_balance_payment(refund):
    """余额支付订单退款成功后，把退款金额退回老板钱包余额（恰好一次）。"""
    if refund.status != Refund.STATUS_SUCCEEDED:
        return None
    payment = refund.payment
    if not payment or payment.channel != BALANCE_CHANNEL:
        return None

    order = refund.order
    profile = getattr(order.boss_user, 'client_profile', None) if order and order.boss_user_id else None
    if not profile:
        logger.warning('[钱包退款] 找不到退款对应的老板资料 refund_no=%s', refund.refund_no)
        return None

    amount = qmoney(refund.amount)
    if amount <= ZERO:
        return None

    with transaction.atomic():
        wallet = get_or_lock_wallet(profile)
        entry, _created = write_wallet_ledger(
            wallet,
            ClientWalletLedger.TYPE_REFUND_IN,
            amount,
            reference_type='refund',
            reference_id=refund.refund_no,
            note=f'退款 {refund.refund_no} 退回钱包余额',
        )
    return entry


def create_manual_wallet_adjustment(*, profile, amount, reason, operator):
    reason = str(reason or '').strip()
    if not reason:
        raise ValidationError({'reason': '人工调整必须填写原因'})
    if not getattr(operator, 'is_authenticated', False):
        raise ValidationError({'operator': '人工调整必须记录操作管理员'})
    amount = qmoney(amount)
    if amount == ZERO:
        raise ValidationError({'amount': '调整金额不能为0'})

    with transaction.atomic():
        wallet = get_or_lock_wallet(profile)
        entry, _created = write_wallet_ledger(
            wallet,
            ClientWalletLedger.TYPE_ADMIN_ADJUST,
            amount,
            reference_type='manual',
            reference_id=f'manual:{uuid.uuid4().hex[:12]}',
            note=reason,
            operator=operator,
        )
    return entry
