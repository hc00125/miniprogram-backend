from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.exceptions import ValidationError

from apps.catalog.models import Package, PackageSpec, PlayerType
from apps.payments.models import Payment, Refund
from apps.payments.services import mark_payment_paid
from apps.players.models import Player

from .designations import accept_designation, decline_designation
from .models import Order, OrderDesignation
from .services import can_player_grab_order, create_order, grab_order


class TargetedPlayerProductTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_user(username='targeted-product-boss')
        self.player_user = User.objects.create_user(username='targeted-product-player')
        self.other_user = User.objects.create_user(username='targeted-product-other')
        self.player_type = PlayerType.objects.create(name='专属服务技术陪', priority=1, price_extra=0)
        self.player = Player.objects.create(
            user=self.player_user,
            name='专属服务陪玩师',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
            is_online=True,
        )
        self.other_player = Player.objects.create(
            user=self.other_user,
            name='公开抢单陪玩师',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
            is_online=True,
        )
        self.package = Package.objects.create(
            name='专属四套四弹',
            base_price=25,
            player_count=1,
            selling_mode=Package.SELLING_MODE_PLAYER_DESIGNATED,
            owner_player=self.player,
            is_active=True,
        )
        self.spec = PackageSpec.objects.create(
            package=self.package,
            name='四套四弹',
            price=25,
            is_active=True,
        )

    def create_targeted_order(self, **overrides):
        payload = {
            'boss_wechat': 'targeted-product-openid',
            'game_id': 'TARGETED-ROOM',
            'package_id': self.package.id,
            'spec_id': self.spec.id,
            'quantity': 3,
            'booked_hours': 3,
        }
        payload.update(overrides)
        return create_order(payload, self.boss)

    def pay_order(self, order):
        payment = Payment.objects.create(
            payment_no=f'TARGETED_PAY_{order.id}',
            order=order,
            channel='wechat_virtual',
            scene='short_series_goods',
            amount=order.total_amount,
            status='paying',
        )
        with patch('apps.orders.targeted_notifications.notify_paid_targeted_order') as notify:
            with self.captureOnCommitCallbacks(execute=True):
                mark_payment_paid(payment, third_trade_no='wx-targeted-paid')
        notify.assert_called_once_with(order.id)

    def test_targeted_product_locks_player_price_and_duration(self):
        order = self.create_targeted_order()

        self.assertEqual(order.fulfillment_mode, Order.FULFILLMENT_MODE_TARGETED)
        self.assertEqual(order.target_player, self.player)
        self.assertEqual(order.required_players, 1)
        self.assertEqual(order.status, Order.STATUS_PENDING_PAYMENT)
        self.assertEqual(order.total_price_per_hour, 25)
        self.assertEqual(order.total_amount, 75)
        self.assertEqual(order.booked_hours, 3)
        self.assertFalse(order.designations.exists())
        self.assertFalse(can_player_grab_order(order, self.other_player))
        with self.assertRaises(ValidationError):
            grab_order(order.order_no, self.other_player, self.other_user)

    def test_client_cannot_append_another_designated_player(self):
        with self.assertRaises(ValidationError) as context:
            self.create_targeted_order(designated_players=[self.other_player.id])

        self.assertIn('商品锁定', str(context.exception.detail))

    def test_payment_creates_single_invitation_then_accept_starts_order(self):
        order = self.create_targeted_order()
        self.pay_order(order)

        order.refresh_from_db()
        invitation = OrderDesignation.objects.get(order=order, player=self.player)
        self.assertTrue(order.paid)
        self.assertEqual(order.status, Order.STATUS_WAITING)
        self.assertEqual(invitation.status, OrderDesignation.STATUS_PENDING)

        accept_designation(order.order_no, self.player, self.player_user)
        order.refresh_from_db()
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, OrderDesignation.STATUS_ACCEPTED)
        self.assertEqual(order.status, Order.STATUS_READY_TO_START)
        self.assertEqual(order.order_players.count(), 1)

    def test_decline_cancels_direct_order_and_creates_refund(self):
        order = self.create_targeted_order()
        self.pay_order(order)

        decline_designation(order.order_no, self.player, self.player_user)
        order.refresh_from_db()
        invitation = OrderDesignation.objects.get(order=order, player=self.player)
        refund = Refund.objects.get(order=order)
        self.assertEqual(invitation.status, OrderDesignation.STATUS_DECLINED)
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        self.assertEqual(refund.status, Refund.STATUS_PENDING)
        self.assertEqual(refund.amount, 75)
