from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order

from .coin_balance_service import pay_order_with_coin_aware_balance
from .models import ClientWallet, ClientWalletLedger, RechargeOrder


@override_settings(WECHAT_VIRTUALPAY_ENV=1, WECHAT_VIRTUALPAY_ENABLED=True, SHARED_SPEND_PLATFORM_APPROVED=True,
                   WECHAT_VIRTUALPAY_COIN_UNITS_PER_YUAN=10)
class CoinCheckoutReservationTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='coin-reservation-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='openid_coin_reservation',
            nickname='Coin预约资金测试老板',
        )
        self.wallet, _ = ClientWallet.objects.update_or_create(profile=self.profile,
            defaults={'balance': Decimal('20.00'), 'recharged_total': Decimal('20.00')})
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

        ClientWalletLedger.objects.create(wallet=self.wallet, entry_type='recharge', amount=Decimal('20'),
            balance_after=Decimal('20'), reference_id=self.recharge.recharge_no)

    def test_credited_checkout_funds_cannot_be_spent_by_another_order(self):
        with self.assertRaises(ValidationError) as context:
            pay_order_with_coin_aware_balance(
                self.other_order.order_no,
                self.user,
            )

        self.assertEqual(str(context.exception.detail['code']), 'CHECKOUT_RECOVERY_PENDING')
        self.wallet.refresh_from_db()
        self.other_order.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('20.00'))
        self.assertFalse(self.other_order.paid)

    def test_original_order_can_consume_its_reserved_checkout_funds(self):
        with patch('apps.wallet.coin_sync.exchange_code_for_session',
                   return_value=(self.profile.openid, 'ephemeral')), \
             patch('apps.wallet.spend_adapter.user_xpay_post',
                   side_effect=lambda endpoint, payload, session, **kw: {'errcode': 0, 'order_id': payload['order_id']}) as external:
            result = pay_order_with_coin_aware_balance(self.original_order.order_no, self.user, code='fresh')
        self.assertEqual(external.call_count, 1)
        self.assertEqual(external.call_args.args[1]['amount'], 200)

        self.assertEqual(result['status'], 'paid')
        self.original_order.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertTrue(self.original_order.paid)
        self.assertEqual(self.wallet.balance, Decimal('0.00'))
