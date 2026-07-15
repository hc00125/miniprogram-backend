from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.catalog.models import Package, PackageSpec, PlayerType
from apps.players.models import Player

from .designations import accept_designation, decline_designation, expire_due_designations
from .models import Order, OrderDesignation, OrderPlayer
from .services import can_player_grab_order, create_order, grab_order


class ConcretePlayerDesignationTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_user(username='designation-boss')
        self.type = PlayerType.objects.create(
            name='娱乐陪',
            priority=1,
            price_extra=20,
        )
        self.other_type = PlayerType.objects.create(
            name='技术陪',
            priority=2,
            price_extra=0,
        )
        self.package = Package.objects.create(
            name='指定测试固定商品',
            base_price=15,
            player_count=1,
            is_active=True,
        )
        self.spec = PackageSpec.objects.create(
            package=self.package,
            name='娱乐陪规格',
            price=15,
            required_player_type=self.type,
            is_active=True,
        )
        self.high_spec = PackageSpec.objects.create(
            package=self.package,
            name='技术陪规格',
            price=25,
            required_player_type=self.other_type,
            is_active=True,
        )
        self.designated_user = User.objects.create_user(username='designated-player-user')
        self.second_designated_user = User.objects.create_user(username='second-designated-player-user')
        self.other_type_user = User.objects.create_user(username='other-type-player-user')
        self.public_user = User.objects.create_user(username='public-player-user')
        self.designated = Player.objects.create(
            user=self.designated_user,
            name='娱乐陪A',
            player_type=self.type,
            status=Player.STATUS_APPROVED,
            is_online=True,
        )
        self.second_designated = Player.objects.create(
            user=self.second_designated_user,
            name='娱乐陪B',
            player_type=self.type,
            status=Player.STATUS_APPROVED,
            is_online=True,
        )
        self.other_type_player = Player.objects.create(
            user=self.other_type_user,
            name='技术陪C',
            player_type=self.other_type,
            status=Player.STATUS_APPROVED,
            is_online=True,
        )
        self.public = Player.objects.create(
            user=self.public_user,
            name='公开娱乐陪D',
            player_type=self.type,
            status=Player.STATUS_APPROVED,
            is_online=True,
        )

    def create_designated_order(self, required_players=1, designated_players=None, spec=None):
        selected_spec = spec or self.spec
        return create_order({
            'boss_wechat': 'designation-boss-openid',
            'game_id': 'TEST-ROOM',
            'package_id': self.package.id,
            'spec_id': selected_spec.id,
            'quantity': 1,
            'required_players': required_players,
            'designated_players': designated_players or [self.designated.id],
            'booked_hours': 1,
        }, self.boss)

    def test_order_creation_keeps_fixed_price_and_creates_invitation(self):
        order = self.create_designated_order()
        invitation = OrderDesignation.objects.get(order=order, player=self.designated)

        self.assertEqual(order.total_amount, 15.0)
        self.assertEqual(order.total_price_per_hour, 15.0)
        self.assertEqual(order.designated_players, [self.designated.id])
        self.assertEqual(invitation.status, OrderDesignation.STATUS_PENDING)
        self.assertEqual(invitation.extra_amount, Decimal('0.00'))
        self.assertGreater(invitation.expires_at, timezone.now())

    def test_higher_tier_can_be_designated_for_lower_tier_spec(self):
        order = self.create_designated_order(designated_players=[self.other_type_player.id])

        self.assertEqual(order.designated_players, [self.other_type_player.id])
        self.assertTrue(OrderDesignation.objects.filter(order=order, player=self.other_type_player).exists())

    def test_lower_tier_cannot_be_designated_for_higher_tier_spec(self):
        with self.assertRaises(ValidationError) as context:
            self.create_designated_order(spec=self.high_spec, designated_players=[self.designated.id])

        self.assertIn('等级不足', str(context.exception.detail))
        self.assertEqual(Order.objects.count(), 0)

    def test_public_player_cannot_take_only_reserved_slot(self):
        order = self.create_designated_order()

        self.assertFalse(can_player_grab_order(order, self.public))
        with self.assertRaises(ValidationError):
            grab_order(order.order_no, self.public)
        self.assertEqual(OrderPlayer.objects.filter(order=order).count(), 0)

    def test_designated_player_accepts_and_order_moves_to_payment(self):
        order = self.create_designated_order()
        accept_designation(order.order_no, self.designated, self.designated_user)

        order.refresh_from_db()
        invitation = OrderDesignation.objects.get(order=order, player=self.designated)
        relation = OrderPlayer.objects.get(order=order, player=self.designated)
        self.assertEqual(invitation.status, OrderDesignation.STATUS_ACCEPTED)
        self.assertTrue(relation.is_designated)
        self.assertEqual(order.status, Order.STATUS_PENDING_PAYMENT)

    def test_two_same_type_players_can_be_designated_together(self):
        order = self.create_designated_order(
            required_players=2,
            designated_players=[self.designated.id, self.second_designated.id],
        )

        self.assertEqual(order.designations.count(), 2)
        self.assertEqual(order.designated_players, [self.designated.id, self.second_designated.id])
        self.assertFalse(can_player_grab_order(order, self.public))

        accept_designation(order.order_no, self.designated, self.designated_user)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_WAITING)

        accept_designation(order.order_no, self.second_designated, self.second_designated_user)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PENDING_PAYMENT)
        self.assertEqual(order.order_players.count(), 2)
        self.assertEqual(order.order_players.filter(is_designated=True).count(), 2)

    def test_mixed_player_types_are_rejected(self):
        with self.assertRaises(ValidationError) as context:
            self.create_designated_order(
                required_players=2,
                designated_players=[self.designated.id, self.other_type_player.id],
            )

        self.assertIn('当前仅支持指定同类型陪玩', str(context.exception.detail))
        self.assertEqual(Order.objects.count(), 0)

    def test_decline_releases_slot_to_public_hall(self):
        order = self.create_designated_order()
        decline_designation(order.order_no, self.designated, self.designated_user)

        order.refresh_from_db()
        invitation = OrderDesignation.objects.get(order=order, player=self.designated)
        self.assertEqual(invitation.status, OrderDesignation.STATUS_DECLINED)
        self.assertIsNone(order.designated_players)
        self.assertTrue(can_player_grab_order(order, self.public))

        grab_order(order.order_no, self.public, self.public_user)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PENDING_PAYMENT)

    def test_expired_invitation_becomes_public_slot(self):
        order = self.create_designated_order()
        invitation = OrderDesignation.objects.get(order=order, player=self.designated)
        invitation.expires_at = timezone.now() - timedelta(seconds=1)
        invitation.save(update_fields=['expires_at'])

        self.assertEqual(expire_due_designations(order=order), 1)
        invitation.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(invitation.status, OrderDesignation.STATUS_EXPIRED)
        self.assertIsNone(order.designated_players)
        self.assertTrue(can_player_grab_order(order, self.public))

    def test_two_player_order_reserves_one_and_leaves_one_public_slot(self):
        order = self.create_designated_order(required_players=2)

        self.assertTrue(can_player_grab_order(order, self.public))
        grab_order(order.order_no, self.public, self.public_user)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_WAITING)
        self.assertEqual(order.order_players.count(), 1)

        accept_designation(order.order_no, self.designated, self.designated_user)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PENDING_PAYMENT)
        self.assertEqual(order.order_players.count(), 2)
