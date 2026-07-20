from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .designated_pricing import calculate_designated_pricing
from .models import Order


PRICING_FIELDS = {'total_price_per_hour', 'total_amount', 'designated_types'}


def should_recalculate(order):
    return bool(
        order.order_type == Order.ORDER_TYPE_NORMAL
        and order.package_id
        and order.spec_id
        and not order.paid
        and order.status in {Order.STATUS_WAITING, Order.STATUS_PENDING_PAYMENT}
    )


def apply_designated_pricing(order):
    if not should_recalculate(order):
        return None
    result = calculate_designated_pricing(order)
    if not result:
        return None
    order.total_price_per_hour = float(result['total'])
    order.total_amount = float(result['total'])
    order.designated_types = result['designated_types']
    return result


@receiver(pre_save, sender=Order, dispatch_uid='calculate_individual_designated_pricing')
def calculate_individual_designated_pricing(sender, instance, **kwargs):
    """
    保存待接单/待支付订单前，按每个指定陪玩的最低指定计费类型分别计算。
    不再把整张订单升级成指定阵容中的最高等级规格。
    """
    apply_designated_pricing(instance)


@receiver(post_save, sender=Order, dispatch_uid='persist_individual_designated_pricing')
def persist_individual_designated_pricing(sender, instance, created, update_fields=None, **kwargs):
    """
    指定邀请拒绝、超时或取消时，旧流程只保存 designated_players 字段。
    pre_save 已经算出新价格，但 Django 会忽略 update_fields 之外的字段，
    因此这里用 queryset.update 原子落库，并避免再次触发信号。
    """
    if created or not should_recalculate(instance):
        return
    if update_fields is None or PRICING_FIELDS.intersection(set(update_fields)):
        return
    result = apply_designated_pricing(instance)
    if not result:
        return
    Order.objects.filter(pk=instance.pk, paid=False).update(
        total_price_per_hour=instance.total_price_per_hour,
        total_amount=instance.total_amount,
        designated_types=instance.designated_types,
    )
