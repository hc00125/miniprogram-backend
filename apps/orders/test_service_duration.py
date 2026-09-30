from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.exceptions import ValidationError

from apps.catalog.models import Package, PackageSpec, PlayerType

from .duration_services import create_cart_orders, create_order
from .models import CartItem


class ServiceDurationOrderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='duration-boss')
        self.player_type = PlayerType.objects.create(name='女陪', priority=10, price_extra=0)
        self.package = Package.objects.create(
            name='女陪',
            product_type=Package.PRODUCT_TYPE_NORMAL,
            player_count=1,
            base_price=35,
            is_active=True,
        )
        self.spec = PackageSpec.objects.create(
            package=self.package,
            name='女陪 六套五弹',
            price=50,
            required_player_type=self.player_type,
            is_active=True,
        )

    def payload(self, quantity=6, booked_hours=6):
        return {
            'boss_wechat': 'openid-duration-boss',
            'game_id': 'team-code',
            'package_id': self.package.id,
            'spec_id': self.spec.id,
            'quantity': quantity,
            'required_players': 1,
            'addon_details': None,
            'designated_players': None,
            'boss_note': '正常开单',
            'booked_hours': booked_hours,
        }

    def test_quantity_creates_one_six_hour_order(self):
        order = create_order(self.payload(), self.user)

        item = order.items.get()
        self.assertEqual(order.required_players, 1)
        self.assertEqual(order.booked_hours, 6)
        self.assertEqual(order.total_price_per_hour, 50)
        self.assertEqual(order.total_amount, 300)
        self.assertEqual(item.quantity, 6)
        self.assertEqual(item.unit_price, 50)
        self.assertEqual(item.amount, 300)
        self.assertIn('预订时长：6小时', order.boss_note)

    def test_quantity_and_booked_hours_must_match(self):
        with self.assertRaisesMessage(ValidationError, '商品数量与预订时长不一致'):
            create_order(self.payload(quantity=6, booked_hours=5), self.user)

    def test_guarantee_product_cannot_select_hours(self):
        guarantee = Package.objects.create(
            name='电视台保底',
            product_type=Package.PRODUCT_TYPE_GUARANTEE,
            player_count=1,
            base_price=58,
            is_active=True,
        )
        with self.assertRaisesMessage(ValidationError, '按单收费'):
            create_order({
                'boss_wechat': 'openid-duration-boss',
                'package_id': guarantee.id,
                'quantity': 6,
                'booked_hours': 6,
            }, self.user)

    def test_cart_quantity_creates_one_order_with_duration(self):
        cart_item = CartItem.objects.create(
            user=self.user,
            package=self.package,
            spec=self.spec,
            spec_id_snapshot=str(self.spec.id),
            spec_name=self.spec.name,
            spec_display_name=self.spec.name,
            price=50,
            quantity=6,
        )

        orders = create_cart_orders([cart_item.id], {
            'boss_wechat': 'openid-duration-boss',
            'game_id': 'team-code',
            'boss_note': '',
        }, self.user)

        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].booked_hours, 6)
        self.assertEqual(orders[0].total_amount, 300)
        self.assertFalse(CartItem.objects.filter(pk=cart_item.pk).exists())
