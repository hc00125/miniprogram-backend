import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.payments.models import Payment

from .models import Order, OrderStatusLog


logger = logging.getLogger(__name__)

PAYMENT_TIMEOUT_MINUTES = 10
PAYMENT_CONFIRMATION_GRACE_SECONDS = 90
PAYMENT_TIMEOUT_REASON = '超过10分钟未完成支付，订单已自动取消'
ACTIVE_PAYMENT_STATUSES = {'created', 'paying'}
REMOTE_PAID = 'paid'
REMOTE_UNPAID = 'unpaid'
REMOTE_UNKNOWN = 'unknown'


def payment_window_started_at(order):
    """Return when the current pending-payment window started.

    Public orders enter pending payment only after the lineup is full, while
    targeted and renewal orders are created directly in pending payment. The
    status log therefore gives all flows one authoritative clock source.
    """
    started_at = (
        OrderStatusLog.objects
        .filter(order=order, to_status=Order.STATUS_PENDING_PAYMENT)
        .order_by('-created_at')
        .values_list('created_at', flat=True)
        .first()
    )
    return started_at or order.created_at


def payment_deadline_at(order):
    if order.status != Order.STATUS_PENDING_PAYMENT or order.paid:
        return None
    return payment_window_started_at(order) + timedelta(minutes=PAYMENT_TIMEOUT_MINUTES)


def payment_confirmation_deadline_at(order):
    deadline = payment_deadline_at(order)
    if not deadline:
        return None
    return deadline + timedelta(seconds=PAYMENT_CONFIRMATION_GRACE_SECONDS)


def payment_deadline_payload(order, now=None):
    now = now or timezone.now()
    deadline = payment_deadline_at(order)
    confirmation_deadline = payment_confirmation_deadline_at(order)
    if not deadline or not confirmation_deadline:
        return {
            'payment_deadline_at': None,
            'payment_confirmation_deadline_at': None,
            'payment_remaining_seconds': 0,
            'payment_confirmation_remaining_seconds': 0,
            'payment_timeout_minutes': PAYMENT_TIMEOUT_MINUTES,
            'payment_confirmation_grace_seconds': PAYMENT_CONFIRMATION_GRACE_SECONDS,
            'payment_phase': 'inactive',
            'can_start_payment': False,
        }

    payment_remaining = max(0, int((deadline - now).total_seconds()))
    confirmation_remaining = max(0, int((confirmation_deadline - now).total_seconds()))
    if now < deadline:
        phase = 'open'
    elif now < confirmation_deadline:
        phase = 'confirming'
    else:
        phase = 'overdue'

    return {
        'payment_deadline_at': deadline,
        'payment_confirmation_deadline_at': confirmation_deadline,
        'payment_remaining_seconds': payment_remaining,
        'payment_confirmation_remaining_seconds': confirmation_remaining,
        'payment_timeout_minutes': PAYMENT_TIMEOUT_MINUTES,
        'payment_confirmation_grace_seconds': PAYMENT_CONFIRMATION_GRACE_SECONDS,
        'payment_phase': phase,
        'can_start_payment': phase == 'open',
    }


def _refresh_active_remote_payment(order):
    """Confirm remote payment state before cancellation.

    A callback can arrive slightly later than the client result. Querying
    WeChat first prevents a paid order from being cancelled. If WeChat cannot
    be queried, return ``unknown`` so the cancellation job retries later.
    """
    active = list(
        Payment.objects
        .filter(order=order, status__in=ACTIVE_PAYMENT_STATUSES)
        .order_by('-created_at')
    )
    if not active:
        return REMOTE_UNPAID

    for payment in active:
        try:
            if payment.channel == 'wechat_virtual':
                from apps.payments.virtualpay import query_virtual_payment

                synced = query_virtual_payment(payment.payment_no, order.boss_user)
            elif payment.channel == 'wechat' and payment.scene == 'jsapi':
                from apps.payments.services import query_wechat_payment

                synced = query_wechat_payment(payment.payment_no, order.boss_user)
            else:
                synced = payment
        except Exception:
            logger.exception(
                '支付超时自动取消前核验失败，暂缓取消 order_no=%s payment_no=%s',
                order.order_no,
                payment.payment_no,
            )
            return REMOTE_UNKNOWN

        if synced.status == 'paid' or getattr(synced.order, 'paid', False):
            return REMOTE_PAID

    return REMOTE_UNPAID


def expire_due_unpaid_order(order, now=None, verify_remote=True):
    """Cancel one unpaid order after its payment and confirmation windows.

    The first 10 minutes are the actual payment window. The following 90
    seconds are only for server-side WeChat reconciliation; the client cannot
    create another payment during this phase.
    """
    now = now or timezone.now()
    if order.status != Order.STATUS_PENDING_PAYMENT or order.paid:
        return False

    deadline = payment_deadline_at(order)
    confirmation_deadline = payment_confirmation_deadline_at(order)
    if not deadline or not confirmation_deadline or deadline > now:
        return False

    remote_state = REMOTE_UNPAID
    if verify_remote:
        remote_state = _refresh_active_remote_payment(order)
        if remote_state in {REMOTE_PAID, REMOTE_UNKNOWN}:
            return False

    # The payment window is closed, but keep the lineup during the short
    # reconciliation grace period so a delayed WeChat result cannot cause a
    # paid-but-cancelled order.
    if confirmation_deadline > now:
        return False

    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status != Order.STATUS_PENDING_PAYMENT or locked.paid:
            return False
        locked_confirmation_deadline = payment_confirmation_deadline_at(locked)
        if not locked_confirmation_deadline or locked_confirmation_deadline > now:
            return False

        from apps.payments.services import close_unpaid_payments_for_order
        from .services import cancel_order

        try:
            close_unpaid_payments_for_order(locked, reason=PAYMENT_TIMEOUT_REASON)
        except Exception:
            logger.exception('关闭超时支付单失败，暂缓取消 order_no=%s', locked.order_no)
            return False

        cancel_order(locked, PAYMENT_TIMEOUT_REASON)
        return True


def expire_due_unpaid_orders(now=None):
    now = now or timezone.now()
    expired = 0
    queryset = (
        Order.objects
        .filter(status=Order.STATUS_PENDING_PAYMENT, paid=False)
        .select_related('boss_user')
        .order_by('id')
    )
    for order in queryset.iterator():
        if expire_due_unpaid_order(order, now=now):
            expired += 1
    return expired


def ensure_payment_window_open(order_no, user=None):
    order = (
        Order.objects
        .filter(order_no=order_no)
        .select_related('boss_user')
        .first()
    )
    if not order:
        raise ValidationError({'detail': '订单不存在'})
    if user is not None and order.boss_user_id != getattr(user, 'id', None) and not getattr(user, 'is_staff', False):
        raise ValidationError({'detail': '无权支付该订单'})

    if order.status == Order.STATUS_PENDING_PAYMENT and not order.paid:
        deadline = payment_deadline_at(order)
        if deadline and deadline <= timezone.now():
            expire_due_unpaid_order(order)
            order.refresh_from_db()
            if order.status == Order.STATUS_CANCELLED:
                raise ValidationError({'detail': '支付时间已超过10分钟，订单已自动取消'})
            raise ValidationError({'detail': '10分钟支付窗口已结束，系统正在核验微信支付结果，请勿重复支付'})
    return order
