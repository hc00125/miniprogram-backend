from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from rest_framework.exceptions import ValidationError

from apps.orders.models import Order, OrderDesignation, OrderPlayer

from .escort_models import OrderEscortRequirementSnapshot
from .escort_qualification import escort_order_block_reason


@receiver(post_save, sender=Order, dispatch_uid='create_order_escort_requirement_snapshot')
def create_order_escort_requirement_snapshot(sender, instance, created, **kwargs):
    if not created:
        return
    package = instance.package
    OrderEscortRequirementSnapshot.objects.get_or_create(
        order=instance,
        defaults={
            'requires_escort_qualification': bool(package.requires_escort_qualification),
            'source_package_id': package.id,
            'source_package_name': package.name,
        },
    )


def _enforce_assignment(order, player):
    reason = escort_order_block_reason(order, player)
    if reason:
        raise ValidationError({'detail': reason})


@receiver(pre_save, sender=OrderPlayer, dispatch_uid='enforce_escort_order_player_qualification')
def enforce_order_player_qualification(sender, instance, **kwargs):
    if instance._state.adding:
        _enforce_assignment(instance.order, instance.player)


@receiver(pre_save, sender=OrderDesignation, dispatch_uid='enforce_escort_designation_qualification')
def enforce_designation_qualification(sender, instance, **kwargs):
    if instance._state.adding:
        reason = escort_order_block_reason(instance.order, instance.player)
        if reason:
            raise ValidationError({'designated_players': reason})
