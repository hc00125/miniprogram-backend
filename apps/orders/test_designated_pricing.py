from django.contrib.auth.models import User
from django.test import TestCase

from apps.catalog.models import Package, PackageSpec, PlayerType
from apps.players.models import Player

from .designations import decline_designation
from .models import Order
from .services import create_order


class DesignatedTierPricingTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_user(username='designated-pricing-boss')
        self.entertainment_type = PlayerType.objects.create(
            name='指定计价娱乐陪', priority=101, price_extra=0,
        )
        self.female_type = PlayerType.objects.create(
            name='指定计价女陪', priority=102, price_extra=0,
        )
        self.technical_type = PlayerType.objects.create(
            name='指定计价技术陪', priority=103, price_extra=0,
        )
        self.gold_type = PlayerType.objects.create(
            name='指定计价金牌陪', priority=104, price_extra=0,
        )
        self.package = Package.objects.create(
            name='三人陪玩指定计价',
            base_price=45,
            player_count=3,
            is_active=True,
        )
        self.entertainment_spec = self.create_spec('三人娱乐陪', '娱乐陪', 45, self.entertainment_type, 10)
        self.female_spec = self.create_spec('三人女陪', '女陪', 60, self.female_type, 20)
        self.technical_spec = self.create_spec('三人技术陪', '技术陪', 75, self.technical_type, 30)
        self.gold_spec = self.create_spec('三人金牌陪', '金牌陪', 105, self.gold_type, 40)

        self.female_player = self.create_player(
            'designated-pricing-female', '女陪A', self.female_type, self.female_type,
        )
        self.gold_player = self.create_player(
            'designated-pricing-gold', '金牌陪B', self.gold_type, self.gold_type,
        )
        # 实际能力是技术陪，但老板指定她时最低只按娱乐陪价计费。
        self.technical_at_entertainment_price = self.create_player(
            'designated-pricing-tech', '技术陪C', self.technical_type, self.entertainment_type,
        )
        self.default_technical_player = self.create_player(
            'designated-pricing-tech-default', '技术陪D', self.technical_type, None,
        )

    def create_spec(self, name, display_name, price, player_type, sort_order):
        return PackageSpec.objects.create(
            package=self.package,
            name=name,
            display_name=display_name,
            price=price,
            required_player_type=player_type,
            sort_order=sort_order,
            is_active=True,
        )

    def create_player(self, username, name, player_type, minimum_billing_type):
        user = User.objects.create_user(username=username)
        return Player.objects.create(
            user=user,
            name=name,
            player_type=player_type,
            minimum_designated_player_type=minimum_billing_type,
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

    def pricing_lines(self, order):
        return {
            item['player_id']: item
            for item in (order.designated_types or [])
            if item.get('source') == 'designated_pricing'
        }

    def test_mixed_designations_charge_each_player_own_minimum_designated_price(self):
        order = self.create_designated_order([
            self.female_player,
            self.gold_player,
            self.technical_at_entertainment_price,
        ])
        order.refresh_from_db()

        # 老板下的仍是三人娱乐陪，不会被改成最高金牌陪规格。
        self.assertEqual(order.spec_id, self.entertainment_spec.id)
        self.assertEqual(order.spec_name_snapshot, '三人娱乐陪')
        self.assertEqual(order.spec_price_snapshot, 45.0)
        # 女陪20 + 金牌35 + 技术陪按娱乐15 = 70。
        self.assertEqual(order.total_price_per_hour, 70.0)
        self.assertEqual(order.total_amount, 70.0)

        lines = self.pricing_lines(order)
        self.assertEqual(lines[self.female_player.id]['effective_billing_type_name'], self.female_type.name)
        self.assertEqual(lines[self.female_player.id]['unit_price'], 20.0)
        self.assertEqual(lines[self.gold_player.id]['effective_billing_type_name'], self.gold_type.name)
        self.assertEqual(lines[self.gold_player.id]['unit_price'], 35.0)
        self.assertEqual(
            lines[self.technical_at_entertainment_price.id]['effective_billing_type_name'],
            self.entertainment_type.name,
        )
        self.assertEqual(lines[self.technical_at_entertainment_price.id]['unit_price'], 15.0)

        item = order.items.get()
        self.assertEqual(item.spec_id, self.entertainment_spec.id)
        self.assertEqual(item.unit_price, 45.0)
        self.assertEqual(item.amount, 45.0)

    def test_blank_minimum_billing_type_falls_back_to_actual_player_type(self):
        order = self.create_designated_order([self.default_technical_player])
        order.refresh_from_db()

        # 技术陪25 + 两个公开娱乐名额15+15。
        self.assertEqual(order.total_amount, 55.0)
        line = self.pricing_lines(order)[self.default_technical_player.id]
        self.assertEqual(line['minimum_billing_type_name'], self.technical_type.name)
        self.assertEqual(line['unit_price'], 25.0)

    def test_declined_designation_reverts_that_slot_to_base_price(self):
        order = self.create_designated_order([self.gold_player, self.technical_at_entertainment_price])
        order.refresh_from_db()
        self.assertEqual(order.total_amount, 65.0)  # 35 + 15 + 15

        decline_designation(order.order_no, self.gold_player, self.gold_player.user)
        order.refresh_from_db()
        self.assertEqual(order.total_amount, 45.0)
        self.assertNotIn(self.gold_player.id, self.pricing_lines(order))

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
        order.refresh_from_db()

        self.assertEqual(order.spec_id, self.entertainment_spec.id)
        self.assertEqual(order.total_amount, 45.0)
        self.assertEqual(order.status, Order.STATUS_WAITING)
