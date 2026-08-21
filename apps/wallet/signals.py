from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.accounts.models import ClientProfile
from apps.payments.models import Refund

from .coin_sync import allocate_refund_coin_amount
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
    """微信返回真实渠道手续费后覆盖保守估算值。"""
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
    """本地余额退款先入账，再标记需要回退到微信官方 coin 的对应份额。

    cancel_currency_pay 需要用户当前 session_key，不能安全地长期保存 session_key。
    因此退款成功时只做确定性分配；用户下一次钻石支付前会先自动同步所有
    pending coin refund，再继续扣款。这样不会出现同一笔官方 coin 被重复消费。
    """
    if instance.status != Refund.STATUS_SUCCEEDED:
        return
    if not instance.payment_id or instance.payment.channel != 'balance':
        return
    refund_balance_payment(instance)
    allocate_refund_coin_amount(instance)
