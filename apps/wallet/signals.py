from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.accounts.models import ClientProfile
from apps.payments.models import Refund

from .models import ClientWallet, RechargeOrder
from .services import refund_balance_payment


CENT = Decimal('0.01')
PERCENT = Decimal('0.01')


@receiver(post_save, sender=ClientProfile)
def create_wallet_for_new_client(sender, instance, created, using, **kwargs):
    """新老板资料创建后补建空钱包，已有钱包时保持幂等。"""
    if not created:
        return

    profile_id = instance.pk

    def ensure_wallet():
        ClientWallet.objects.using(using).get_or_create(profile_id=profile_id)

    transaction.on_commit(ensure_wallet, using=using)


@receiver(post_save, sender=RechargeOrder)
def record_actual_recharge_channel_fee(sender, instance, **kwargs):
    """When WeChat returns the real platform fee, replace the conservative estimate.

    The configured percentage is only a pre-payment safety estimate.  query_order
    may return platform_fee_fen after settlement; using the real number makes the
    wallet's weighted fee reserve and later margin protection more accurate.
    """
    if instance.status != RechargeOrder.STATUS_CREDITED:
        return
    payload = dict(instance.notify_payload or {})
    order_data = payload.get('query_order') or {}
    if not isinstance(order_data, dict):
        return
    raw_fee = order_data.get('platform_fee_fen')
    if raw_fee in (None, ''):
        return
    try:
        fee_yuan = (Decimal(str(raw_fee)) / Decimal('100')).quantize(CENT, rounding=ROUND_HALF_UP)
        amount = Decimal(str(instance.amount or 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    except Exception:
        return
    if fee_yuan < 0 or amount <= 0:
        return

    fee_percent = (fee_yuan * Decimal('100') / amount).quantize(PERCENT, rounding=ROUND_HALF_UP)
    settlement = (amount - fee_yuan).quantize(CENT, rounding=ROUND_HALF_UP)
    payload['actual_platform_fee_yuan'] = str(fee_yuan)
    payload['platform_fee_percent'] = str(fee_percent)
    payload['estimated_platform_fee_yuan'] = str(fee_yuan)
    payload['estimated_settlement_yuan'] = str(settlement)
    RechargeOrder.objects.filter(pk=instance.pk).update(notify_payload=payload)
    instance.notify_payload = payload


@receiver(post_save, sender=Refund)
def sync_balance_refund(sender, instance, **kwargs):
    """余额支付退款的兜底入账；其他渠道由各自退款服务显式处理。"""
    if instance.status != Refund.STATUS_SUCCEEDED:
        return
    if not instance.payment_id or instance.payment.channel != 'balance':
        return
    refund_balance_payment(instance)
