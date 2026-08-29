from decimal import Decimal

from django.test import TestCase
from rest_framework.exceptions import ValidationError

from apps.catalog.models import Package, PackageSpec, PlayerType
from apps.players.models import Player

from .models import Order, OrderDesignation
from .services import create_order


class DesignatedPlayerPricingTests(TestCase):
    def setUp(self):
        self.entertainment_type = PlayerType.objects.create(name='娱乐陪', priority=1)
        self.technical_type = PlayerType.objects.create(name='技术陪', priority=2)
        self.package = Package.objects.create(
            name='四套四弹',
            base_price=15,
            player_count=1,
            is_active=True,
        )
        self.entertainment_spec = PackageSpec.objects.create(
            package=self.package,
            name='四套四弹 +娱乐',
            display_name='娱乐',
            price=15,
            required_player_type=self.entertainment_type,
            is_active=True,
        )
        self.technical_spec = PackageSpec.objects.create(
            package=self.package,
            name='四套四弹 +技术',
            display_name='技术',
            price=25,
            required_player_type=self.technical_type,
            is_active=True,
        )
        self.entertainment_player = Player.objects.create(
            name='娱乐陪测试',
            player_type=self.entertainment_type,
            status=Player.STATUS_APPROVED,
            is_online=True,
        )
        self.technical_player = Player.objects.create(
            name='技术陪测试',
            player_type=self.technical_type,
            status=Player.STATUS_APPROVED,
            is_online=True,
        )

    def payload(self, spec, player):
        return {
            'boss_wechat': 'boss-openid-p0',
            'game_id': 'team-001',
            'package_id': self.package.id,
            'spec_id': spec.id,
            'quantity': 1,
            'required_players': 1,
            'designated_players': [player.id],
            'booked_hours': 1,
        }

    def test_technical_player_uses_technical_slot_price_without_changing_base_spec(self):
        order = create_order(self.payload(self.entertainment_spec, self.technical_player))
        designation = OrderDesignation.objects.get(order=order, player=self.technical_player)

        self.assertEqual(Decimal(designation.extra_amount), Decimal('0.00'))
        self.assertEqual(order.spec_id, self.entertainment_spec.id)
        self.assertEqual(Decimal(str(order.total_amount)), Decimal('25.00'))
        pricing_lines = [
            item for item in (order.designated_types or [])
            if item.get('source') == 'designated_pricing'
        ]
        self.assertEqual(len(pricing_lines), 1)
        self.assertEqual(pricing_lines[0]['effective_billing_type_name'], self.technical_type.name)

    def test_entertainment_player_cannot_use_technical_spec(self):
        with self.assertRaises(ValidationError) as context:
            create_order(self.payload(self.technical_spec, self.entertainment_player))

        self.assertIn('不满足该订单要求', str(context.exception.detail))
        self.assertEqual(Order.objects.count(), 0)

    def test_matching_spec_creates_zero_fee_designation(self):
        order = create_order(self.payload(self.technical_spec, self.technical_player))
        designation = OrderDesignation.objects.get(order=order, player=self.technical_player)
        self.assertEqual(Decimal(designation.extra_amount), Decimal('0.00'))
        self.assertEqual(Decimal(str(order.total_amount)), Decimal('25.00'))

    def test_entertainment_player_can_use_entertainment_spec(self):
        order = create_order(self.payload(self.entertainment_spec, self.entertainment_player))
        self.assertEqual(Decimal(str(order.total_amount)), Decimal('15.00'))
        self.assertEqual(order.designations.count(), 1)