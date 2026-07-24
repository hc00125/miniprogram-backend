from django.db.models.signals import pre_save
from django.dispatch import receiver

from apps.orders.models import Order
from apps.orders.payment_deadlines import payment_deadline_at

from .models import Payment


@receiver(pre_save, sender=Payment)
def clamp_payment_expiry_to_business_deadline(sender, instance, **kwargs):
    """Never let a payment outlive the order's ten-minute reservation.

    Payment creation code may ask for a fresh ten-minute payment object even
    when the boss enters the cashier near the end of the business window. The
    persisted order deadline is authoritative, so clamp ``expires_at`` before
    ordinary WeChat JSAPI uses it as ``time_expire`` and before virtual-payment
    reuse checks read it.
    """
    if not instance.order_id or instance.status not in {'created', 'paying'}:
        return

    order = getattr(instance, 'order', None)
    if order is None or order.pk != instance.order_id:
        order = Order.objects.filter(pk=instance.order_id).first()
    if not order or order.status != Order.STATUS_PENDING_PAYMENT or order.paid:
        return

    deadline = payment_deadline_at(order)
    if deadline and (instance.expires_at is None or instance.expires_at > deadline):
        instance.expires_at = deadline
