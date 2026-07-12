from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.orders.models import Order

from .settlements import create_order_earnings


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
    create_order_earnings(instance, completed_at=instance.end_time)
