from django.db.models.signals import pre_save
from django.dispatch import receiver

from apps.orders.models import Order
from apps.orders.payment_deadlines import payment_deadline_at

from .models import Payment


@receiver(pre_save, sender=Payment)
def clamp_payment_expiry_to_business_deadline(sender, instance, **kwargs):
    """Never let a payment outlive the order's ten-minute reservation.

    ``Payment.order`` targets ``Order.order_no``, so ``instance.order_id`` is
    the business order number rather than the numeric primary key.
    """
    if not instance.order_id or instance.status not in {'created', 'paying'}:
        return

    try:
        order = instance.order
    except Order.DoesNotExist:
        order = Order.objects.filter(order_no=instance.order_id).first()
    if not order or order.status != Order.STATUS_PENDING_PAYMENT or order.paid:
        return

    deadline = payment_deadline_at(order)
    if deadline and (instance.expires_at is None or instance.expires_at > deadline):
        instance.expires_at = deadline
