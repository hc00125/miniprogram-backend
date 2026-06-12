import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.orders.models import Order
from .models import Payment, PaymentCallbackLog
from .wechatpay import WechatPayClient, WechatPayError

PAYMENT_EXPIRE_MINUTES = 10


def generate_payment_no():
    return f"PAY{timezone.localtime().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:8].upper()}"


def get_order_amount(order):
    raw_amount = order.total_amount if order.total_amount is not None else order.total_price_per_hour
    try:
        return Decimal(str(raw_amount or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({'detail': '订单金额不正确'}) from exc


def amount_to_cents(amount):
    return int((Decimal(str(amount)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def ensure_order_owner(order, user):
    if not user or not getattr(user, 'is_authenticated', False):
        raise PermissionDenied('请先登录')
    if not order.boss_user_id:
        raise PermissionDenied('该订单未绑定当前登录用户，不能发起在线支付')
    if order.boss_user_id != user.id:
        raise PermissionDenied('无权支付该订单')


def get_payment_for_user(payment_no, user):
    payment = Payment.objects.filter(payment_no=payment_no).select_related('order').first()
    if not payment:
        raise ValidationError({'detail': '支付单不存在'})
    ensure_order_owner(payment.order, user)
    return payment


def create_payment(order_no, channel):
    if channel not in {'wechat', 'alipay'}:
        raise ValidationError({'detail': '支付渠道不正确'})
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        raise ValidationError({'detail': '订单不存在'})
    if order.paid or order.status == Order.STATUS_COMPLETED:
        raise ValidationError({'detail': '订单已支付'})
    if order.status != Order.STATUS_PENDING_PAYMENT:
        raise ValidationError({'detail': '当前订单状态不可支付'})
    amount = get_order_amount(order)
    if amount <= 0:
        raise ValidationError({'detail': '订单金额不正确'})

    now = timezone.now()
    existing = Payment.objects.filter(
        order=order,
        channel=channel,
        status='paying',
        expires_at__gt=now,
    ).order_by('-created_at').first()
    if existing:
        return existing

    return Payment.objects.create(
        payment_no=generate_payment_no(),
        order=order,
        channel=channel,
        scene='native' if channel == 'wechat' else 'precreate',
        amount=float(amount),
        status='paying',
        qr_code=f'mockpay://{channel}/{order.order_no}',
        expires_at=now + timedelta(minutes=PAYMENT_EXPIRE_MINUTES),
    )


def build_mock_request_payment(payment):
    return {
        'timeStamp': str(int(timezone.now().timestamp())),
        'nonceStr': uuid.uuid4().hex,
        'package': f'prepay_id=mock_{payment.payment_no}',
        'signType': 'RSA',
        'paySign': uuid.uuid4().hex,
        'payment_no': payment.payment_no,
        'order_no': payment.order_id,
        'amount': payment.amount,
        'status': payment.status,
        'prepay_id': f'mock_{payment.payment_no}',
        'mock': True,
    }


def _build_real_request_payment(client, payment):
    payload = client.build_miniprogram_payment_params(payment.third_order_no)
    payload.update({
        'payment_no': payment.payment_no,
        'order_no': payment.order_id,
        'amount': payment.amount,
        'status': payment.status,
        'prepay_id': payment.third_order_no,
        'mock': False,
    })
    return payload


def create_miniprogram_payment(order_no, user=None, code=None, openid=None):
    order = (
        Order.objects
        .filter(order_no=order_no)
        .select_related('package', 'boss_user')
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
    if amount <= 0:
        raise ValidationError({'detail': '订单金额不正确'})

    profile = getattr(user, 'client_profile', None)
    payer_openid = getattr(profile, 'openid', '') or ''
    if settings.ENABLE_MOCK_PAYMENT and not payer_openid:
        payer_openid = openid or ''
    if not payer_openid:
        raise ValidationError({'detail': '当前用户缺少微信 openid，请重新登录小程序'})

    now = timezone.now()
    existing = Payment.objects.filter(
        order=order,
        channel='wechat',
        scene='jsapi',
        status='paying',
        expires_at__gt=now,
    ).order_by('-created_at').first()

    if settings.ENABLE_MOCK_PAYMENT:
        if existing:
            return existing, build_mock_request_payment(existing)
        payment = Payment.objects.create(
            payment_no=generate_payment_no(),
            order=order,
            channel='wechat',
            scene='jsapi',
            amount=float(amount),
            status='paying',
            third_order_no=f'mock_prepay_{uuid.uuid4().hex[:16]}',
            notify_payload={'mock': True},
            expires_at=now + timedelta(minutes=PAYMENT_EXPIRE_MINUTES),
        )
        return payment, build_mock_request_payment(payment)

    client = WechatPayClient()
    if existing:
        if not existing.third_order_no:
            raise ValidationError({'detail': '支付单正在生成，请稍后重试'})
        return existing, _build_real_request_payment(client, existing)

    payment = Payment.objects.create(
        payment_no=generate_payment_no(),
        order=order,
        channel='wechat',
        scene='jsapi',
        amount=float(amount),
        status='paying',
        notify_payload={'mock': False},
        expires_at=now + timedelta(minutes=PAYMENT_EXPIRE_MINUTES),
    )

    description_prefix = settings.WECHATPAY_DESCRIPTION_PREFIX.strip()
    package_name = getattr(order.package, 'name', '') or '小程序订单'
    description = f'{description_prefix}{package_name}'[:127]
    try:
        result = client.create_jsapi_order(
            out_trade_no=payment.payment_no,
            description=description,
            amount_total=amount_to_cents(amount),
            openid=payer_openid,
            time_expire=timezone.localtime(payment.expires_at).isoformat(timespec='seconds'),
            attach=order.order_no,
        )
        prepay_id = result.get('prepay_id')
        if not prepay_id:
            raise WechatPayError('微信支付下单响应缺少 prepay_id')
    except Exception as exc:
        payment.status = 'failed'
        payment.notify_payload = {'mock': False, 'create_error': str(exc)[:500]}
        payment.updated_at = timezone.now()
        payment.save(update_fields=['status', 'notify_payload', 'updated_at'])
        raise

    payment.third_order_no = prepay_id
    payment.updated_at = timezone.now()
    payment.save(update_fields=['third_order_no', 'updated_at'])
    return payment, _build_real_request_payment(client, payment)


@transaction.atomic
def mark_payment_paid(payment, third_trade_no='', payload=None):
    payment = (
        Payment.objects
        .select_for_update()
        .select_related('order')
        .get(pk=payment.pk)
    )
    if payment.status == 'paid':
        return payment

    paid_at = timezone.now()
    payment.status = 'paid'
    payment.third_trade_no = third_trade_no or payment.third_trade_no
    payment.notify_payload = payload or {}
    payment.paid_at = paid_at
    payment.updated_at = paid_at
    payment.save(update_fields=[
        'status',
        'third_trade_no',
        'notify_payload',
        'paid_at',
        'updated_at',
    ])

    order = payment.order
    order.paid = True
    order.payment_method = payment.channel
    order.payment_confirmed_at = paid_at
    order_update_fields = ['paid', 'payment_method', 'payment_confirmed_at']
    if order.status == Order.STATUS_PENDING_PAYMENT:
        order.status = Order.STATUS_COMPLETED
        order_update_fields.append('status')
    order.save(update_fields=order_update_fields)
    return payment


def _safe_transaction_payload(transaction_data, notification_id=''):
    amount = transaction_data.get('amount') or {}
    return {
        'notification_id': notification_id,
        'appid': transaction_data.get('appid', ''),
        'mchid': transaction_data.get('mchid', ''),
        'out_trade_no': transaction_data.get('out_trade_no', ''),
        'transaction_id': transaction_data.get('transaction_id', ''),
        'trade_type': transaction_data.get('trade_type', ''),
        'trade_state': transaction_data.get('trade_state', ''),
        'success_time': transaction_data.get('success_time', ''),
        'amount': {
            'total': amount.get('total'),
            'payer_total': amount.get('payer_total'),
            'currency': amount.get('currency', ''),
        },
    }


def validate_wechat_transaction(payment, transaction_data):
    client_appid = transaction_data.get('appid')
    client_mchid = transaction_data.get('mchid')
    if client_appid != settings.WECHAT_APP_ID:
        raise WechatPayError('支付回调 appid 不匹配')
    if client_mchid != settings.WECHATPAY_MCH_ID:
        raise WechatPayError('支付回调 mchid 不匹配')
    if transaction_data.get('out_trade_no') != payment.payment_no:
        raise WechatPayError('支付回调商户订单号不匹配')
    if transaction_data.get('trade_state') != 'SUCCESS':
        raise WechatPayError('微信支付订单尚未支付成功')

    amount = transaction_data.get('amount') or {}
    if amount.get('currency') not in {None, '', 'CNY'}:
        raise WechatPayError('支付回调币种不正确')
    if amount.get('total') != amount_to_cents(payment.amount):
        raise WechatPayError('支付回调金额不匹配')
    attach = transaction_data.get('attach')
    if attach and attach != payment.order_id:
        raise WechatPayError('支付回调附加订单号不匹配')
    return True


def handle_wechat_notification(headers, raw_body):
    client = WechatPayClient()
    envelope = client.verify_callback(headers, raw_body)
    notification_id = envelope.get('id', '')
    if envelope.get('event_type') != 'TRANSACTION.SUCCESS':
        log_callback(
            'wechat',
            {'notification_id': notification_id, 'event_type': envelope.get('event_type', '')},
            verified=True,
            result='ignored event type',
        )
        return None

    transaction_data = client.decrypt_resource(envelope.get('resource') or {})
    payment_no = transaction_data.get('out_trade_no', '')
    payment = Payment.objects.filter(payment_no=payment_no).select_related('order').first()
    if not payment:
        log_callback(
            'wechat',
            _safe_transaction_payload(transaction_data, notification_id),
            payment_no,
            verified=True,
            result='payment not found',
        )
        raise WechatPayError('本地支付单不存在')

    validate_wechat_transaction(payment, transaction_data)
    safe_payload = _safe_transaction_payload(transaction_data, notification_id)
    payment = mark_payment_paid(
        payment,
        third_trade_no=transaction_data.get('transaction_id', ''),
        payload=safe_payload,
    )
    log_callback(
        'wechat',
        safe_payload,
        payment_no,
        verified=True,
        result='paid' if payment.status == 'paid' else payment.status,
    )
    return payment


def query_wechat_payment(payment_no, user):
    payment = get_payment_for_user(payment_no, user)
    if settings.ENABLE_MOCK_PAYMENT or payment.status == 'paid':
        return payment
    if payment.channel != 'wechat' or payment.scene != 'jsapi':
        raise ValidationError({'detail': '该支付单不是微信小程序支付'})

    client = WechatPayClient()
    transaction_data = client.query_order(payment.payment_no)
    trade_state = transaction_data.get('trade_state', '')
    if trade_state == 'SUCCESS':
        validate_wechat_transaction(payment, transaction_data)
        return mark_payment_paid(
            payment,
            third_trade_no=transaction_data.get('transaction_id', ''),
            payload=_safe_transaction_payload(transaction_data),
        )

    status_map = {
        'CLOSED': 'closed',
        'REVOKED': 'closed',
        'PAYERROR': 'failed',
    }
    local_status = status_map.get(trade_state)
    if local_status and payment.status != local_status:
        payment.status = local_status
        payment.notify_payload = _safe_transaction_payload(transaction_data)
        payment.updated_at = timezone.now()
        payment.save(update_fields=['status', 'notify_payload', 'updated_at'])
    return payment


def log_callback(channel, payload, payment_no='', verified=False, result=''):
    return PaymentCallbackLog.objects.create(
        payment_no=payment_no,
        channel=channel,
        payload=payload,
        verify_result=verified,
        handled_result=result,
    )
