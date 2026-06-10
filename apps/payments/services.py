import math
import uuid
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.orders.models import Order
from .models import Payment, PaymentCallbackLog

PAYMENT_EXPIRE_MINUTES = 10


def generate_payment_no():
    return f"PAY{timezone.localtime().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:8].upper()}"


def get_order_amount(order):
    return round(float(order.total_amount or order.total_price_per_hour or 0), 2)


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
    existing = Payment.objects.filter(order=order, channel=channel, status='paying', expires_at__gt=now).order_by('-created_at').first()
    if existing:
        return existing

    payment = Payment.objects.create(
        payment_no=generate_payment_no(),
        order=order,
        channel=channel,
        scene='native' if channel == 'wechat' else 'precreate',
        amount=amount,
        status='paying',
        qr_code=f'mockpay://{channel}/{order.order_no}',
        expires_at=now + timedelta(minutes=PAYMENT_EXPIRE_MINUTES),
    )
    return payment


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
    }


def create_miniprogram_payment(order_no, code=None, openid=None):
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        raise ValidationError({'detail': '订单不存在'})
    if order.paid or order.status == Order.STATUS_COMPLETED:
        raise ValidationError({'detail': '订单已支付'})
    if order.status != Order.STATUS_PENDING_PAYMENT:
        raise ValidationError({'detail': '当前订单状态不可支付'})
    if not code and not openid and not settings.ENABLE_MOCK_PAYMENT:
        raise ValidationError({'detail': '缺少微信登录 code'})

    now = timezone.now()
    existing = Payment.objects.filter(order=order, channel='wechat', scene='jsapi', status='paying', expires_at__gt=now).order_by('-created_at').first()
    if existing:
        return existing, build_mock_request_payment(existing)

    payment = Payment.objects.create(
        payment_no=generate_payment_no(),
        order=order,
        channel='wechat',
        scene='jsapi',
        amount=get_order_amount(order),
        status='paying',
        third_order_no=f'mock_prepay_{uuid.uuid4().hex[:16]}',
        notify_payload={'openid': openid or '', 'code': bool(code), 'mock': True},
        expires_at=now + timedelta(minutes=PAYMENT_EXPIRE_MINUTES),
    )
    return payment, build_mock_request_payment(payment)


def mark_payment_paid(payment, third_trade_no='mock_paid', payload=None):
    order = payment.order
    if payment.status == 'paid':
        return payment
    payment.status = 'paid'
    payment.third_trade_no = third_trade_no
    payment.notify_payload = payload or {}
    payment.paid_at = timezone.now()
    payment.save(update_fields=['status', 'third_trade_no', 'notify_payload', 'paid_at', 'updated_at'])

    order.paid = True
    order.status = Order.STATUS_COMPLETED
    order.payment_method = payment.channel
    order.payment_confirmed_at = payment.paid_at
    order.save(update_fields=['paid', 'status', 'payment_method', 'payment_confirmed_at'])
    return payment


def log_callback(channel, payload, payment_no='', verified=False, result=''):
    return PaymentCallbackLog.objects.create(
        payment_no=payment_no,
        channel=channel,
        payload=payload,
        verify_result=verified,
        handled_result=result,
    )
