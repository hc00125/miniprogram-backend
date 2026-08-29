from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package, PackageSpec, PlayerType
from apps.players.models import Player
from apps.wallet.models import RechargeOrder

from .models import Order, OrderStatusLog
from .payment_deadlines import ensure_payment_window_open, persist_payment_window
from .services import create_order, grab_order


class LateCreditedCheckoutRecoveryTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_user(username='late-checkout-boss')
        self.player_user = User.objects.create_user(username='late-checkout-player')
        self.player_type = PlayerType.objects.create(name='迟到支付测试陪练', priority=952)
        self.package = Package.objects.create(
            name='迟到支付测试商品',
            base_price=15,
            player_count=1,
            is_active=True,
        )
        self.spec = PackageSpec.objects.create(
            package=self.package,
            name='迟到支付测试规格',
            price=15,
            required_player_type=self.player_type,
            is_active=True,
        )
        self.player = Player.objects.create(
            user=self.player_user,
            name='迟到支付测试陪玩',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
            is_online=True,
            total_orders=0,
        )
        self.profile = ClientProfile.objects.create(
            user=self.boss,
            openid='late-checkout-openid',
            nickname='迟到支付老板',
        )

    def create_order(self):
        order = create_order({
            'boss_wechat': self.profile.openid,
            'game_id': 'LATE-CHECKOUT-ROOM',
            'package_id': self.package.id,
            'spec_id': self.spec.id,
            'quantity': 1,
            'required_players': 1,
            'booked_hours': 1,
        }, self.boss)
        return grab_order(order.order_no, self.player, self.player_user)

    def backdate(self, order, minutes=12):
        started_at = timezone.now() - timedelta(minutes=minutes)
        log = (
            OrderStatusLog.objects
            .filter(order=order, to_status=Order.STATUS_PENDING_PAYMENT)
            .order_by('-created_at')
            .first()
        )
        OrderStatusLog.objects.filter(pk=log.pk).update(created_at=started_at)
        persist_payment_window(order, started_at=started_at, force_reset=True)

    def recharge(self, order, status):
        return RechargeOrder.objects.create(
            recharge_no=f'LATE-{status}-{order.order_no}',
            profile=self.profile,
            product=None,
            amount=Decimal('15.00'),
            channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
            status=status,
            checkout_order_no=order.order_no,
            expires_at=timezone.now() + timedelta(minutes=5),
            notify_payload={
                'mode': 'short_series_coin',
                'checkout_order_no': order.order_no,
                'wechat_coin_units_per_yuan': 10,
            },
        )

    def test_credited_checkout_can_use_expired_window_only_for_recovery(self):
        order = self.create_order()
        self.backdate(order)
        self.recharge(order, RechargeOrder.STATUS_CREDITED)

        recovered = ensure_payment_window_open(
            order.order_no,
            self.boss,
            allow_credited_checkout_recovery=True,
        )

        self.assertEqual(recovered.pk, order.pk)
        recovered.refresh_from_db()
        self.assertFalse(recovered.paid)
        self.assertEqual(recovered.status, Order.STATUS_PENDING_PAYMENT)

    def test_normal_payment_guard_still_blocks_expired_credited_checkout(self):
        order = self.create_order()
        self.backdate(order)
        self.recharge(order, RechargeOrder.STATUS_CREDITED)

        with self.assertRaises(ValidationError):
            ensure_payment_window_open(order.order_no, self.boss)

    def test_paying_checkout_cannot_use_recovery_bypass(self):
        order = self.create_order()
        self.backdate(order)
        self.recharge(order, RechargeOrder.STATUS_PAYING)

        with self.assertRaises(ValidationError):
            ensure_payment_window_open(
                order.order_no,
                self.boss,
                allow_credited_checkout_recovery=True,
            )
