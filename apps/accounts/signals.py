from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.orders.models import Order
from apps.payments.models import Refund

from .vip import record_completed_order, record_successful_refund


@receiver(post_save, sender=Order)
def sync_order_consumption(sender, instance, **kwargs):
    record_completed_order(instance)


@receiver(post_save, sender=Refund)
def sync_refund_consumption(sender, instance, **kwargs):
    record_successful_refund(instance)
