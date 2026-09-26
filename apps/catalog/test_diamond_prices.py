from django.core.exceptions import ValidationError
from django.test import TestCase

from .models import PlayerType


class CatalogDiamondPriceValidationTests(TestCase):
    def test_price_that_maps_to_integer_diamonds_is_allowed(self):
        player_type = PlayerType.objects.create(name='整数钻石测试类型', price_extra=0.1)
        self.assertEqual(player_type.price_extra, 0.1)

    def test_price_that_produces_fractional_diamonds_is_rejected(self):
        with self.assertRaises(ValidationError):
            PlayerType.objects.create(name='小数钻石测试类型', price_extra=0.11)
