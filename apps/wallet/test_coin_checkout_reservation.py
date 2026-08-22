from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.exceptions import ValidationError

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order

from .coin_balance_service import pay_order_with_coin_aware_balance
from .models import ClientWallet, RechargeOrder


class CoinCheckoutReservationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='coin-reservation-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='openid_coin_reservation',
            nickname='Coin预约资金测试老板',
        )
        self.wallet = ClientWallet.objects.create(
            profile=self.profile,
            balance=Decimal('20.00'),
            recharged_total=Decimal('20.00'),
        )
        self.package = Package.objects.create(
            name='Coin预约资金测试商品',
            player_count=1,
            base_price=20,
        )
        self.original_order = Order.objects.create(
            order_no='COINRESERVEORIGINAL1',
            boss_user=self.user,
            boss_wechat='coin-reservation-boss',
            package=self.package,
            required_players=1,
            total_price_per_hour=20,
            total_amount=20,
            status=Order.STATUS_PENDING_PAYMENT,
        )
        self.other_order = Order.objects.create(
            order_no='COINRESERVEOTHER001',
            boss_user=self.user,
            boss_wechat='coin-reservation-boss',
            package=self.package,
            required_players=1,
            total_price_per_hour=20,
            total_amount=20,
            status=Order.STATUS_PENDING_PAYMENT,
        )
        self.recharge = RechargeOrder.objects.create(
            recharge_no='RCGCOINRESERVE001',
            profile=self.profile,
            product=None,
            amount=Decimal('20.00'),
            channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
            status=RechargeOrder.STATUS_CREDITED,
            checkout_order_no=self.original_order.order_no,
            notify_payload={
                'mode': 'short_series_coin',
                'checkout_order_no': self.original_order.order_no,
                'wechat_coin_units_per_yuan': 10,
            },
        )

    def test_credited_checkout_funds_cannot_be_spent_by_another_order(self):
        with self.assertRaises(ValidationError) as context:
            pay_order_with_coin_aware_balance(
                self.other_order.order_no,
                self.user,
            )

        self.assertIn('另一笔已付款订单确认', str(context.exception.detail))
        self.wallet.refresh_from_db()
        self.other_order.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('20.00'))
        self.assertFalse(self.other_order.paid)

    def test_original_order_can_consume_its_reserved_checkout_funds(self):
        prepared = {
            'wechat_coin_diamonds': '200.0',
            'wechat_coin_units': 200,
            'wechat_coin_units_per_yuan': 10,
            '_session_key': 'session-key',
            '_user_ip': '127.0.0.1',
        }
        executed = {
            'wechat_coin_diamonds': '200.0',
            'wechat_coin_units': 200,
            'wechat_coin_units_per_yuan': 10,
            'wechat_coin_status': 'succeeded',
        }
        with patch(
            'apps.wallet.coin_balance_service.prepare_coin_spend',
            return_value=prepared,
        ), patch(
            'apps.wallet.coin_balance_service.execute_coin_spend',
            return_value=executed,
        ):
            result = pay_order_with_coin_aware_balance(
                self.original_order.order_no,
                self.user,
            )

        self.assertEqual(result['status'], 'paid')
        self.original_order.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertTrue(self.original_order.paid)
        self.assertEqual(self.wallet.balance, Decimal('0.00'))
