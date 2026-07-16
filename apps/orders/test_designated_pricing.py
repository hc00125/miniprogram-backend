from django.contrib.auth.models import User
from django.test import TestCase

from apps.catalog.models import Package, PackageSpec, PlayerType
from apps.players.models import Player

from .models import Order
from .services import create_order


class DesignatedTierPricingTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_user(username='designated-pricing-boss')
        self.entertainment_type = PlayerType.objects.create(
            name='指定计价娱乐陪',
            priority=101,
            price_extra=0,
        )
        self.technical_type = PlayerType.objects.create(
            name='指定计价技术陪',
            priority=102,
            price_extra=0,
        )
        self.package = Package.objects.create(
            name='三人陪玩指定计价',
            base_price=45,
            player_count=3,
            is_active=True,
        )
        self.entertainment_spec = PackageSpec.objects.create(
            package=self.package,
            name='三人娱乐陪',
            display_name='娱乐陪',
            price=45,
            required_player_type=self.entertainment_type,
            sort_order=10,
            is_active=True,
        )
        self.technical_spec = PackageSpec.objects.create(
            package=self.package,
            name='三人技术陪',
            display_name='技术陪',
            price=75,
            required_player_type=self.technical_type,
            sort_order=20,
            is_active=True,
        )
        self.technical_players = [
            self.create_player(f'designated-pricing-tech-{index}', f'技术陪{index}', self.technical_type)
            for index in range(1, 4)
        ]
        self.entertainment_players = [
            self.create_player(f'designated-pricing-fun-{index}', f'娱乐陪{index}', self.entertainment_type)
            for index in range(1, 4)
        ]

    def create_player(self, username, name, player_type):
        user = User.objects.create_user(username=username)
        return Player.objects.create(
            user=user,
            name=name,
            player_type=player_type,
            status=Player.STATUS_APPROVED,
            is_online=True,
        )

    def create_designated_order(self, players):
        return create_order({
            'boss_wechat': 'designated-pricing-openid',
            'game_id': 'DESIGNATED-PRICE',
            'package_id': self.package.id,
            'spec_id': self.entertainment_spec.id,
            'quantity': 1,
            'required_players': 3,
            'designated_players': [player.id for player in players],
            'booked_hours': 1,
        }, self.boss)

    def test_three_technical_designations_upgrade_entertainment_order_to_technical_price(self):
        order = self.create_designated_order(self.technical_players)
        order.refresh_from_db()

        self.assertEqual(order.spec_id, self.technical_spec.id)
        self.assertEqual(order.spec_name_snapshot, '技术陪')
        self.assertEqual(order.spec_price_snapshot, 75.0)
        self.assertEqual(order.required_players, 3)
        self.assertEqual(order.total_price_per_hour, 75.0)
        self.assertEqual(order.total_amount, 75.0)
        self.assertEqual(order.designated_types, [{
            'type_id': self.technical_type.id,
            'count': 3,
            'source': 'spec',
        }])
        self.assertIn('指定陪玩等级计价', order.boss_note)

        item = order.items.get()
        self.assertEqual(item.spec_id, self.technical_spec.id)
        self.assertEqual(item.spec_display_name, '技术陪')
        self.assertEqual(item.unit_price, 75.0)
        self.assertEqual(item.amount, 75.0)
        self.assertEqual(item.quantity, 1)

    def test_same_tier_designations_keep_selected_entertainment_price(self):
        order = self.create_designated_order(self.entertainment_players)
        order.refresh_from_db()

        self.assertEqual(order.spec_id, self.entertainment_spec.id)
        self.assertEqual(order.total_amount, 45.0)
        self.assertEqual(order.designated_types, [{
            'type_id': self.entertainment_type.id,
            'count': 3,
            'source': 'spec',
        }])
        self.assertNotIn('指定陪玩等级计价', order.boss_note or '')

    def test_public_high_tier_grab_does_not_change_non_designated_order_price(self):
        order = create_order({
            'boss_wechat': 'public-pricing-openid',
            'game_id': 'PUBLIC-PRICE',
            'package_id': self.package.id,
            'spec_id': self.entertainment_spec.id,
            'quantity': 1,
            'required_players': 3,
            'booked_hours': 1,
        }, self.boss)

        self.assertEqual(order.spec_id, self.entertainment_spec.id)
        self.assertEqual(order.total_amount, 45.0)
        self.assertEqual(order.status, Order.STATUS_WAITING)
