from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order
from apps.wallet.models import RechargeOrder


class CoinCheckoutRecoveryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='coin-recovery-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='openid_coin_recovery',
            nickname='Coin恢复测试老板',
        )
        self.package = Package.objects.create(
            name='Coin恢复测试商品',
            player_count=1,
            base_price=60,
        )
        self.order = Order.objects.create(
            order_no='COINRECOVERYORDER01',
            boss_user=self.user,
            boss_wechat='coin-recovery-boss',
            package=self.package,
            required_players=1,
            total_price_per_hour=60,
            total_amount=60,
            status=Order.STATUS_PENDING_PAYMENT,
        )
        self.recharge = RechargeOrder.objects.create(
            recharge_no='RCGCOINRECOVERY001',
            profile=self.profile,
            product=None,
            amount=Decimal('60.00'),
            channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
            status=RechargeOrder.STATUS_CREDITED,
            checkout_order_no=self.order.order_no,
            notify_payload={
                'mode': 'short_series_coin',
                'checkout_order_no': self.order.order_no,
                'wechat_coin_units_per_yuan': 10,
            },
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_credited_checkout_is_found_without_payment_row(self):
        response = self.client.post(
            f'/api/pay/wechat/virtual/query-order/{self.order.order_no}',
            {},
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['found'])
        self.assertEqual(response.data['status'], RechargeOrder.STATUS_CREDITED)
        self.assertEqual(response.data['payment_no'], self.recharge.recharge_no)
        self.assertEqual(response.data['checkout_order_no'], self.order.order_no)
        self.assertFalse(self.order.payments.exists())

    def test_json_fallback_recovers_row_created_during_rolling_deploy(self):
        RechargeOrder.objects.filter(pk=self.recharge.pk).update(checkout_order_no='')

        response = self.client.post(
            f'/api/pay/wechat/virtual/query-order/{self.order.order_no}',
            {},
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['found'])
        self.assertEqual(response.data['payment_no'], self.recharge.recharge_no)
