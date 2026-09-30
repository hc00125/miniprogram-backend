from django.db.models.signals import pre_save
from django.dispatch import receiver

from apps.wallet.diamonds import validate_integer_diamond_amount

from .models import Addon, Package, PackageSpec, PlayerType


def _validate_fields(instance, field_names):
    for field_name in field_names:
        value = getattr(instance, field_name, None)
        if value is None:
            continue
        validate_integer_diamond_amount(value)


@receiver(pre_save, sender=PlayerType)
def validate_player_type_prices(sender, instance, **kwargs):
    _validate_fields(instance, ('price_extra',))


@receiver(pre_save, sender=Package)
def validate_package_prices(sender, instance, **kwargs):
    _validate_fields(instance, ('base_price', 'original_price'))


@receiver(pre_save, sender=PackageSpec)
def validate_package_spec_prices(sender, instance, **kwargs):
    _validate_fields(instance, ('price', 'original_price'))


@receiver(pre_save, sender=Addon)
def validate_addon_prices(sender, instance, **kwargs):
    _validate_fields(instance, ('price_per_player',))
