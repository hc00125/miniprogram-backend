from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package, PlayerType
from apps.orders.models import Order, OrderPlayer
from apps.payments.models import Payment
from apps.players.models import Player

from .models import OrderCommissionOverride, PlayerEarning


class PlatformFeeMarginProtectionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='fee-owner')
        ClientProfile.objects.create(
            user=self.owner,
            openid='openid-fee-owner',
            nickname='渠道费测试老板',
        )
        self.player_user = User.objects.create_user(username='fee-player')
        self.player_type = PlayerType.objects.create(name='渠道费测试陪玩', priority=1)
        self.package = Package.objects.create(name='渠道费测试套餐', player_count=1, base_price=100)
        self.player = Player.objects.create(
            user=self.player_user,
            name='渠道费测试陪玩',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
        )

    def _order(self, suffix, platform):
        order = Order.objects.create(
            order_no=f'FEE{suffix}',
            boss_user=self.owner,
            boss_wechat='fee-boss',
            package=self.package,
            package_name_snapshot='渠道费测试套餐',
            required_players=1,
            total_price_per_hour=Decimal('100.00'),
            total_amount=Decimal('100.00'),
            status=Order.STATUS_IN_PROGRESS,
            paid=True,
            order_type=Order.ORDER_TYPE_NORMAL,
        )
        OrderPlayer.objects.create(order=order, player=self.player)
        Payment.objects.create(
            payment_no=f'PAY{suffix}',
            order=order,
            channel='wechat_virtual',
            scene='short_series_goods',
            amount=Decimal('100.00'),
            status='paid',
            paid_at=timezone.now(),
            notify_payload={'client_platform': platform},
        )
        return order

    def _complete(self, order):
        order.status = Order.STATUS_COMPLETED
        order.end_time = timezone.now()
        order.save(update_fields=['status', 'end_time'])
        order.refresh_from_db()

    def test_ios_default_keeps_fifteen_percent_net_margin(self):
        order = self._order('IOS001', 'ios')
        self._complete(order)

        override = OrderCommissionOverride.objects.get(order=order)
        earning = PlayerEarning.objects.get(order=order, player=self.player)
        self.assertEqual(override.commission_rate, Decimal('30.00'))
        self.assertEqual(earning.commission_rate, Decimal('30.00'))
        self.assertEqual(earning.commission_amount, Decimal('300.00'))
        self.assertEqual(earning.net_amount, Decimal('700.00'))

        payment = order.payments.get(payment_no='PAYIOS001')
        self.assertEqual(payment.notify_payload['platform_fee_percent'], '15.00')
        self.assertEqual(payment.notify_payload['target_net_margin_percent'], '15.00')

    def test_non_ios_default_keeps_existing_sixteen_percent_commission(self):
        order = self._order('AND001', 'android')
        self._complete(order)

        override = OrderCommissionOverride.objects.get(order=order)
        earning = PlayerEarning.objects.get(order=order, player=self.player)
        self.assertEqual(override.commission_rate, Decimal('16.00'))
        self.assertEqual(earning.commission_rate, Decimal('16.00'))

    def test_manual_commission_override_is_never_replaced(self):
        order = self._order('MAN001', 'ios')
        OrderCommissionOverride.objects.create(
            order=order,
            commission_rate=Decimal('20.00'),
            reason='老板手工活动价',
            created_by=self.owner,
        )
        self._complete(order)

        override = OrderCommissionOverride.objects.get(order=order)
        earning = PlayerEarning.objects.get(order=order, player=self.player)
        self.assertEqual(override.commission_rate, Decimal('20.00'))
        self.assertEqual(override.reason, '老板手工活动价')
        self.assertEqual(earning.commission_rate, Decimal('20.00'))
