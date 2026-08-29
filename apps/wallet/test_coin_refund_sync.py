from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order
from apps.payments.models import Payment, Refund

from .coin_sync import allocate_refund_coin_amount, sync_pending_coin_refunds


VIRTUAL_SETTINGS = {
    'WECHAT_VIRTUALPAY_ENABLED': True,
    'WECHAT_APP_ID': 'wx_test_appid',
    'WECHAT_APP_SECRET': 'test_secret',
    'WECHAT_VIRTUALPAY_OFFER_ID': 'test_offer',
    'WECHAT_VIRTUALPAY_APP_KEY': 'test_app_key',
    'WECHAT_VIRTUALPAY_ENV': 0,
    'WECHAT_VIRTUALPAY_HTTP_TIMEOUT': 10,
    'WECHAT_VIRTUALPAY_COIN_UNITS_PER_YUAN': 100,
    'ENABLE_MOCK_PAYMENT': False,
}


@override_settings(**VIRTUAL_SETTINGS)
class CoinRefundSynchronizationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='coin-refund-sync-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='openid_coin_refund_sync',
            nickname='Coin退款同步测试',
        )
        package = Package.objects.create(
            name='Coin退款同步商品',
            player_count=1,
            base_price=20,
        )
        self.order = Order.objects.create(
            order_no='COINREFUNDSYNC001',
            boss_user=self.user,
            boss_wechat='coin-refund-sync',
            package=package,
            required_players=1,
            total_price_per_hour=20,
            total_amount=20,
            status=Order.STATUS_READY_TO_START,
            paid=True,
        )
        self.payment = Payment.objects.create(
            payment_no='PAYCOINREFUNDSYNC001',
            order=self.order,
            channel='balance',
            scene='wallet',
            amount=Decimal('20.00'),
            status='paid',
            notify_payload={
                'balance_payment': True,
                'wechat_coin_status': 'succeeded',
                'wechat_coin_order_id': 'CPPAYORIGINAL001',
                'wechat_coin_units': 2000,
                'wechat_coin_units_per_yuan': 100,
                'wechat_coin_diamonds': '200.0',
                'wechat_coin_amount_yuan': '20.00',
            },
        )

    def _refund(self, number, amount):
        refund = Refund.objects.create(
            refund_no=number,
            payment=self.payment,
            order=self.order,
            amount=Decimal(amount),
            status=Refund.STATUS_SUCCEEDED,
        )
        allocate_refund_coin_amount(refund)
        refund.refresh_from_db()
        return refund

    def test_cent_partial_refund_allocates_exact_units_without_rounding(self):
        refund = self._refund('RFCOINEXACT001', '5.37')
        payload = refund.notify_payload
        self.assertEqual(payload['wechat_coin_refund_units'], 537)
        self.assertEqual(payload['wechat_coin_refund_diamonds'], '53.7')
        self.assertEqual(payload['wechat_coin_units_per_yuan'], 100)
        self.assertEqual(payload['wechat_coin_refund_status'], 'pending')

    def test_two_partial_local_refunds_issue_only_one_remote_full_cancel(self):
        first = self._refund('RFCOINPART001', '5.37')
        second = self._refund('RFCOINPART002', '3.21')

        with patch(
            'apps.wallet.coin_sync.user_xpay_post',
            return_value={'errcode': 0, 'errmsg': 'OK'},
        ) as xpay:
            synced = sync_pending_coin_refunds(
                self.profile,
                'fresh-session',
                '127.0.0.1',
            )

        self.assertEqual(synced, 2)
        self.assertEqual(xpay.call_count, 1)
        endpoint, request_payload, session_key = xpay.call_args.args
        self.assertEqual(endpoint, '/xpay/cancel_currency_pay')
        self.assertEqual(request_payload['pay_order_id'], 'CPPAYORIGINAL001')
        # WeChat only allows one cancel, so the first remote operation restores
        # the entire original coin-backed payment, not only the first ¥5.37.
        self.assertEqual(request_payload['amount'], 2000)
        self.assertEqual(session_key, 'fresh-session')
        self.assertEqual(
            xpay.call_args.kwargs['extra_success_codes'],
            {268490005},
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(first.notify_payload['wechat_coin_refund_status'], 'succeeded')
        self.assertEqual(second.notify_payload['wechat_coin_refund_status'], 'succeeded')
        self.assertTrue(second.notify_payload['wechat_coin_refund_reused_remote_full_cancel'])
        self.assertEqual(
            self.payment.notify_payload['wechat_coin_remote_refund_status'],
            'succeeded',
        )
        self.assertEqual(
            self.payment.notify_payload['wechat_coin_remote_refund_amount_units'],
            2000,
        )

    def test_retry_after_remote_success_can_recover_from_already_refunded_state(self):
        refund = self._refund('RFCOINCRASH001', '4.19')

        # user_xpay_post normally normalizes 268490005 to success only because
        # coin_sync explicitly opts into that endpoint-specific recovery code.
        with patch(
            'apps.wallet.coin_sync.user_xpay_post',
            return_value={'errcode': 268490005, 'errmsg': 'already refunded'},
        ) as xpay:
            synced = sync_pending_coin_refunds(self.profile, 'fresh-session')

        self.assertEqual(synced, 1)
        self.assertEqual(
            xpay.call_args.kwargs['extra_success_codes'],
            {268490005},
        )
        refund.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(refund.notify_payload['wechat_coin_refund_status'], 'succeeded')
        self.assertEqual(
            self.payment.notify_payload['wechat_coin_remote_refund_status'],
            'succeeded',
        )
