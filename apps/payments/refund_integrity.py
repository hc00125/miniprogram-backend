import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Payment, Refund


logger = logging.getLogger(__name__)
CENT = Decimal('0.01')
BALANCE_CHANNEL = 'balance'
ACTIVE_REFUND_STATUSES = (
    Refund.STATUS_PENDING,
    Refund.STATUS_PROCESSING,
    Refund.STATUS_SUCCEEDED,
)
SETTLEABLE_REFUND_STATUSES = (
    Refund.STATUS_PENDING,
    Refund.STATUS_PROCESSING,
    Refund.STATUS_SUCCEEDED,
)


def _qmoney(value):
    try:
        return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({'detail': '退款金额不正确'}) from exc


def _operator_or_none(operator):
    return operator if getattr(operator, 'is_authenticated', False) else None


def _settle_balance_refund_side_effects(refund, operator=None):
    """Run idempotent non-wallet side effects after the customer money is safe."""
    try:
        from apps.accounts.vip import record_successful_refund

        record_successful_refund(refund, operator=operator)
    except Exception:
        logger.exception(
            '[余额退款] 累计消费/VIP冲销失败，退款已到账，等待后续对账 refund_no=%s',
            refund.refund_no,
        )

    try:
        from apps.earnings.services import reverse_refund_earnings

        reverse_refund_earnings(refund, operator=operator)
    except Exception:
        logger.exception(
            '[余额退款] 陪玩工资冲销失败，退款已到账，等待后续对账 refund_no=%s',
            refund.refund_no,
        )


def settle_balance_refund(refund_or_id, operator=None):
    """Immediately return a balance-channel refund to the boss wallet.

    PostgreSQL must only lock concrete rows owned by the refund/payment tables.
    In particular, ``Order.boss_user`` is nullable, so joining it into a
    ``SELECT ... FOR UPDATE`` query creates a LEFT OUTER JOIN that PostgreSQL
    refuses to lock.  Related rows needed for display/account lookup are loaded
    without broadening the lock target.
    """
    refund_id = getattr(refund_or_id, 'pk', refund_or_id)
    with transaction.atomic():
        refund = (
            Refund.objects
            .select_for_update(of=('self',))
            .select_related('payment', 'order')
            .get(pk=refund_id)
        )
        payment = (
            Payment.objects
            .select_for_update(of=('self',))
            .get(pk=refund.payment_id)
        )
        refund.payment = payment

        if payment.channel != BALANCE_CHANNEL:
            return refund
        if refund.status not in SETTLEABLE_REFUND_STATUSES:
            raise ValidationError({'detail': '当前退款状态不能自动退回钱包'})

        amount = _qmoney(refund.amount)
        if amount <= 0:
            raise ValidationError({'detail': '退款金额必须大于0'})

        order = refund.order
        boss_user = order.boss_user if order and order.boss_user_id else None
        profile = getattr(boss_user, 'client_profile', None) if boss_user else None
        if not profile:
            raise ValidationError({'detail': '余额退款找不到对应老板钱包账户'})

        # The wallet ledger is the money source of truth.  The same refund_no
        # can only be credited once, so retries are safe.
        if refund.status != Refund.STATUS_SUCCEEDED:
            payload = refund.notify_payload if isinstance(refund.notify_payload, dict) else {}
            payload = {
                **payload,
                'balance_refund': {
                    'automatic': True,
                    'settled_at': timezone.now().isoformat(),
                },
            }
            Refund.objects.filter(pk=refund.pk).update(
                status=Refund.STATUS_SUCCEEDED,
                failed_reason='',
                third_refund_no=refund.third_refund_no or refund.refund_no,
                notify_payload=payload,
                updated_at=timezone.now(),
            )
            refund.status = Refund.STATUS_SUCCEEDED
            refund.failed_reason = ''
            refund.third_refund_no = refund.third_refund_no or refund.refund_no
            refund.notify_payload = payload

        from apps.wallet.services import refund_balance_payment

        entry = refund_balance_payment(refund)
        if entry is None:
            raise ValidationError({'detail': '余额退款入账失败，请联系管理员处理'})

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

    refund = Refund.objects.select_related('payment', 'order').get(pk=refund_id)
    _settle_balance_refund_side_effects(
        refund,
        operator=_operator_or_none(operator) or refund.created_by,
    )
    return refund


def create_refund(payment_no, amount, reason='', operator=None):
    """Concurrency-safe refund creation with immediate balance settlement."""
    from .services import generate_refund_no

    with transaction.atomic():
        payment = (
            Payment.objects
            .select_for_update(of=('self',))
            .select_related('order')
            .filter(payment_no=payment_no)
            .first()
        )
        if not payment:
            raise ValidationError({'detail': '支付单不存在'})
        if payment.status != 'paid':
            raise ValidationError({'detail': '只有已支付订单可以退款'})

        refund_amount = _qmoney(amount)
        if refund_amount <= 0:
            raise ValidationError({'detail': '退款金额必须大于 0'})

        existing_refunds = list(
            Refund.objects
            .select_for_update(of=('self',))
            .filter(payment=payment, status__in=ACTIVE_REFUND_STATUSES)
        )
        refunded_amount = sum((_qmoney(item.amount) for item in existing_refunds), Decimal('0.00'))
        payment_amount = _qmoney(payment.amount)
        if refunded_amount + refund_amount > payment_amount:
            raise ValidationError({'detail': '退款金额不能超过可退金额'})

        refund = Refund.objects.create(
            refund_no=generate_refund_no(),
            payment=payment,
            order=payment.order,
            amount=refund_amount,
            reason=reason or '',
            status=Refund.STATUS_PENDING,
            created_by=_operator_or_none(operator),
        )

        if payment.channel == BALANCE_CHANNEL:
            refund = settle_balance_refund(refund.pk, operator=operator)
        return refund


def ensure_payment_refund(payment_no, target_amount, reason='', operator=None):
    """Ensure a cumulative refund amount exists without duplicating old refunds.

    This is the cancellation-path API.  Historical balance refunds stuck in
    pending/processing are settled first, then only the remaining amount is
    created.  For external payment channels an existing pending refund counts
    toward the requested target and is reused instead of duplicated.
    """
    with transaction.atomic():
        payment = (
            Payment.objects
            .select_for_update(of=('self',))
            .filter(payment_no=payment_no)
            .first()
        )
        if not payment:
            raise ValidationError({'detail': '支付单不存在'})

        target = _qmoney(target_amount)
        if target <= 0:
            raise ValidationError({'detail': '退款金额必须大于 0'})
        if target > _qmoney(payment.amount):
            raise ValidationError({'detail': '退款金额不能超过支付金额'})

        active = list(
            Refund.objects
            .select_for_update(of=('self',))
            .filter(payment=payment, status__in=ACTIVE_REFUND_STATUSES)
            .order_by('created_at', 'id')
        )

        if payment.channel == BALANCE_CHANNEL:
            for item in active:
                if item.status in {Refund.STATUS_PENDING, Refund.STATUS_PROCESSING}:
                    settle_balance_refund(item.pk, operator=operator)
            active = list(
                Refund.objects
                .select_for_update(of=('self',))
                .filter(payment=payment, status=Refund.STATUS_SUCCEEDED)
                .order_by('created_at', 'id')
            )
            settled = sum((_qmoney(item.amount) for item in active), Decimal('0.00'))
            if settled > target:
                raise ValidationError({'detail': '历史退款金额已超过本次应退金额，请人工核对'})
            remaining = target - settled
            if remaining <= 0:
                return active[-1] if active else None
            if payment.status == 'refunded':
                raise ValidationError({'detail': '支付记录已标记全额退款，但退款金额不完整，请人工核对'})
            return create_refund(payment.payment_no, remaining, reason=reason, operator=operator)

        active_total = sum((_qmoney(item.amount) for item in active), Decimal('0.00'))
        if active_total > target:
            raise ValidationError({'detail': '已有退款金额超过本次应退金额，请人工核对'})
        if active_total == target and active:
            return active[-1]
        remaining = target - active_total
        return create_refund(payment.payment_no, remaining, reason=reason, operator=operator)


def install_refund_service_patch():
    """Install the unified service without rewriting the large legacy module."""
    from . import services

    if getattr(services.create_refund, '_balance_refund_integrity_patch', False):
        return

    patched = wraps(services.create_refund)(create_refund)
    patched._balance_refund_integrity_patch = True
    services.create_refund = patched
