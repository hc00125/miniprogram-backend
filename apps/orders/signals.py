from decimal import Decimal

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from rest_framework.exceptions import ValidationError

from apps.common.money import money

from .designated_pricing import calculate_designated_pricing
from .models import Order, OrderDesignation


PRICING_FIELDS = {'total_price_per_hour', 'total_amount', 'designated_types'}


def should_recalculate(order):
    return bool(
        order.fulfillment_mode == Order.FULFILLMENT_MODE_PUBLIC
        and
        order.pricing_mode != Order.PRICING_MODE_COMPOSITION
        and
        order.order_type == Order.ORDER_TYPE_NORMAL
        and order.package_id
        and order.spec_id
        and not order.paid
        and order.status in {Order.STATUS_WAITING, Order.STATUS_PENDING_PAYMENT}
    )


def should_recalculate_composition(order):
    return bool(
        order.fulfillment_mode == Order.FULFILLMENT_MODE_PUBLIC
        and
        order.pricing_mode == Order.PRICING_MODE_COMPOSITION
        and order.order_type == Order.ORDER_TYPE_NORMAL
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

    per_hour = money(result['total'])
    booked_hours = Decimal(str(order.booked_hours or 1))
    order.total_price_per_hour = float(per_hour)
    order.total_amount = float(money(per_hour * booked_hours))
    order.designated_types = result['designated_types']
    return result


@receiver(pre_save, sender=Order, dispatch_uid='calculate_individual_designated_pricing')
def calculate_individual_designated_pricing(sender, instance, **kwargs):
    """
    保存待接单/待支付订单前，按每个指定陪玩的最低指定计费类型分别计算。
    total_price_per_hour 保存每小时费用，total_amount 再乘预订时长。
    """
    # Composition orders have a different invariant: their price must come
    # from one static CompositionSku.  Repricing is persisted post-save when
    # a designation changes so legacy signals never overwrite that snapshot.
    if (
        instance.pricing_mode == Order.PRICING_MODE_COMPOSITION
        or instance.fulfillment_mode == Order.FULFILLMENT_MODE_TARGETED
    ):
        return
    apply_designated_pricing(instance)


@receiver(post_save, sender=Order, dispatch_uid='persist_individual_designated_pricing')
def persist_individual_designated_pricing(sender, instance, created, update_fields=None, **kwargs):
    """
    指定邀请拒绝、超时或取消时，旧流程只保存 designated_players 字段。
    pre_save 已经算出新价格，但 Django 会忽略 update_fields 之外的字段，
    因此这里用 queryset.update 原子落库，并避免再次触发信号。
    """
    if created:
        return
    if should_recalculate_composition(instance):
        if update_fields is None or PRICING_FIELDS.intersection(set(update_fields)):
            return
        from .composition_pricing import mark_composition_pricing_unavailable, reprice_composition_order

        try:
            reprice_composition_order(instance, require_virtual_binding=False)
        except ValidationError as exc:
            # Declining/releasing/expiring an invitation is a player-facing
            # state change and must succeed even if operations forgot to build
            # the fallback static combination SKU.  The marker removes the
            # payment mapping, so this never falls through to legacy pricing.
            mark_composition_pricing_unavailable(instance, exc)
        return
    if not should_recalculate(instance):
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


@receiver(post_save, sender=OrderDesignation, dispatch_uid='notify_public_designation_wechat')
def notify_public_designation_wechat(sender, instance, created, **kwargs):
    """普通指定邀请创建后，在事务提交成功后发送一次微信订阅消息。"""
    if not created or instance.status != OrderDesignation.STATUS_PENDING:
        return
    if instance.order.fulfillment_mode == Order.FULFILLMENT_MODE_TARGETED:
        # 专属商品订单由支付成功流程触发，避免重复发送。
        return

    from .targeted_notifications import notify_designation

    transaction.on_commit(lambda designation_id=instance.id: notify_designation(designation_id))
