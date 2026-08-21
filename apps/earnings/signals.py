from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.common.platform_context import current_client_platform
from apps.orders.models import Order
from apps.payments.models import Payment, Refund

from .adjustments import reverse_refund_earnings
from .platform_fees import protect_order_margin
from .settlements import create_order_earnings


@receiver(post_save, sender=Payment)
def remember_virtual_payment_platform(sender, instance, created, **kwargs):
    """Persist device platform on a new virtual payment for later async settlement.

    The create endpoint is wrapped in capture_client_platform.  Persisting the
    platform means a later query/webhook still knows whether Apple channel cost
    applies, even if the completion request comes from another device/context.
    """
    if not created or instance.channel != 'wechat_virtual':
        return
    payload = dict(instance.notify_payload or {})
    if payload.get('client_platform'):
        return
    platform = current_client_platform()
    payload['client_platform'] = platform
    Payment.objects.filter(pk=instance.pk).update(notify_payload=payload)
    instance.notify_payload = payload


@receiver(post_save, sender=Order)
def create_earnings_after_order_completion(sender, instance, **kwargs):
    if not (
        instance.order_type == Order.ORDER_TYPE_NORMAL
        and instance.status == Order.STATUS_COMPLETED
        and instance.paid
    ):
        return
    if not instance.order_players.exists():
        return

    # 在工资生成前把支付渠道成本计入订单抽成。默认目标净毛利15%；
    # iOS按保守15%渠道费时自动得到约30%总抽成，非iOS约16%。
    protect_order_margin(instance)
    create_order_earnings(instance, completed_at=instance.end_time)


@receiver(post_save, sender=Refund)
def reverse_earnings_after_refund_success(sender, instance, **kwargs):
    if instance.status != Refund.STATUS_SUCCEEDED:
        return
    reverse_refund_earnings(instance)
