from django.test import TestCase
from rest_framework.exceptions import ValidationError

from apps.catalog.models import Package, PlayerType
from apps.players.escort_models import OrderEscortRequirementSnapshot
from apps.players.models import Player, PlayerEscortQualification

from .models import Order
from .services import create_order, grab_order


class EscortOrderQualificationTests(TestCase):
    def setUp(self):
        self.player_type = PlayerType.objects.create(name='技术陪', priority=2)
        self.escort_package = Package.objects.create(
            name='护航测试商品',
            base_price=30,
            player_count=1,
            is_active=True,
            requires_escort_qualification=True,
        )
        self.normal_package = Package.objects.create(
            name='普通测试商品',
            base_price=20,
            player_count=1,
            is_active=True,
            requires_escort_qualification=False,
        )
        self.unqualified_player = Player.objects.create(
            name='未认证陪玩',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
        )
        self.qualified_player = Player.objects.create(
            name='护航陪玩',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
        )
        PlayerEscortQualification.objects.create(
            player=self.qualified_player,
            status=PlayerEscortQualification.STATUS_APPROVED,
        )

    def payload(self, package, boss='boss-escort-test', designated_players=None):
        return {
            'boss_wechat': boss,
            'package_id': package.id,
            'required_players': 1,
            'booked_hours': 1,
            'designated_players': designated_players or [],
        }

    def test_new_order_captures_product_requirement_snapshot(self):
        order = create_order(self.payload(self.escort_package))
        snapshot = OrderEscortRequirementSnapshot.objects.get(order=order)
        self.assertTrue(snapshot.requires_escort_qualification)
        self.assertEqual(snapshot.source_package_id, self.escort_package.id)

        self.escort_package.requires_escort_qualification = False
        self.escort_package.save(update_fields=['requires_escort_qualification'])
        snapshot.refresh_from_db()
        self.assertTrue(snapshot.requires_escort_qualification)

    def test_unqualified_player_cannot_grab_escort_order(self):
        order = create_order(self.payload(self.escort_package))
        with self.assertRaises(ValidationError) as context:
            grab_order(order.order_no, self.unqualified_player)
        self.assertIn('护航资格已通过', str(context.exception.detail))
        self.assertEqual(order.order_players.count(), 0)

    def test_approved_escort_player_can_use_original_grab_flow(self):
        order = create_order(self.payload(self.escort_package))
        result = grab_order(order.order_no, self.qualified_player)
        self.assertEqual(result.status, Order.STATUS_PENDING_PAYMENT)
        self.assertTrue(order.order_players.filter(player=self.qualified_player).exists())

    def test_normal_order_does_not_require_escort_qualification(self):
        order = create_order(self.payload(self.normal_package))
        result = grab_order(order.order_no, self.unqualified_player)
        self.assertEqual(result.status, Order.STATUS_PENDING_PAYMENT)

    def test_unqualified_player_cannot_be_designated_for_escort_order(self):
        with self.assertRaises(ValidationError) as context:
            create_order(self.payload(
                self.escort_package,
                designated_players=[self.unqualified_player.id],
            ))
        self.assertIn('护航资格已通过', str(context.exception.detail))
        self.assertEqual(Order.objects.count(), 0)

    def test_qualified_player_can_be_designated_for_escort_order(self):
        order = create_order(self.payload(
            self.escort_package,
            designated_players=[self.qualified_player.id],
        ))
        self.assertEqual(order.designations.count(), 1)
        self.assertEqual(order.designations.first().player_id, self.qualified_player.id)
