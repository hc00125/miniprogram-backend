from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.orders.models import Order
from apps.payments.models import Refund

from .vip import apply_vip_kook_room_to_order, record_completed_order, record_successful_refund


@receiver(post_save, sender=Order)
def sync_order_consumption(sender, instance, created=False, **kwargs):
    if created and instance.order_type == Order.ORDER_TYPE_NORMAL:
        apply_vip_kook_room_to_order(instance)
    record_completed_order(instance)


@receiver(post_save, sender=Refund)
def sync_refund_consumption(sender, instance, **kwargs):
    record_successful_refund(instance)
