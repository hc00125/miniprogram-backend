from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.payments.models import Refund

from .services import refund_balance_payment


@receiver(post_save, sender=Refund)
def sync_balance_refund(sender, instance, **kwargs):
    """退款成功且原支付通道是余额时，把钱退回老板钱包。幂等靠账本唯一约束。"""
    if instance.status != Refund.STATUS_SUCCEEDED:
        return
    refund_balance_payment(instance)
