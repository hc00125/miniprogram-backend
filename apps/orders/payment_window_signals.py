from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Order, OrderStatusLog


@receiver(post_save, sender=OrderStatusLog)
def persist_window_on_pending_payment_log(sender, instance, created, **kwargs):
    if not created or instance.to_status != Order.STATUS_PENDING_PAYMENT:
        return

    from .payment_deadlines import persist_payment_window

    persist_payment_window(
        instance.order,
        started_at=instance.created_at,
        force_reset=True,
    )
