from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order
from apps.wallet.models import ClientWallet, ClientWalletLedger

from .models import Payment, Refund
from .refund_integrity import create_refund as create_wallet_refund
from .wechat_virtual_refunds import (
    cash_refund_status,
    refund_payment_to_wechat_original,
    sync_wechat_original_refund,
)


VIRTUAL_SETTINGS = {
    'WECHAT_VIRTUALPAY_ENABLED': True,
    'WECHAT_VIRTUALPAY_ENV': 0,
    'WECHAT_VIRTUALPAY_OFFER_ID': 'test_offer',
    'WECHAT_VIRTUALPAY_APP_KEY': 'test_app_key',
    'WECHAT_APP_ID': 'wx_test_appid',
    'WECHAT_APP_SECRET': 'test_secret',
}


@override_settings(**VIRTUAL_SETTINGS)
class UnifiedDiamondRefundTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='diamond-refund-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='openid_diamond_refund_boss',
            nickname='统一钻石退款测试老板',
        )
        self.wallet = ClientWallet.objects.create(
            profile=self.profile,
            balance=Decimal('50.00'),
            recharged_total=Decimal('50.00'),
        )
        self.package = Package.objects.create(
            name='统一钻石模型套餐',
            player_count=1,
            base_price=30,
        )
        self.order = Order.objects.create(
            order_no='DIAMOND_ORDER_001',
            boss_user=self.user,
            boss_wechat='diamond_refund_boss',
            package=self.package,
            required_players=1,
            total_price_per_hour=30,
            total_amount=30,
            status=Order.STATUS_READY_TO_START,
            paid=True,
            payment_method='wechat_virtual',
        )
        self.payment = Payment.objects.create(
            payment_no='DIAMOND_PAY_001',
            order=self.order,
            channel='wechat_virtual',
            scene='short_series_goods',
            amount=Decimal('30.00'),
            status='paid',
            third_order_no='DIAMOND_PAY_001',
            notify_payload={'virtual_payment': True},
        )

    def test_wechat_direct_payment_is_hidden_credit_then_diamond_spend(self):
        self.wallet.refresh_from_db()
        self.payment.refresh_from_db()

        self.assertEqual(self.wallet.balance, Decimal('50.00'))
        credit = ClientWalletLedger.objects.get(
            entry_type=ClientWalletLedger.TYPE_VIRTUAL_PURCHASE_CREDIT,
            reference_id=self.payment.payment_no,
        )
        spend = ClientWalletLedger.objects.get(
            entry_type=ClientWalletLedger.TYPE_VIRTUAL_ORDER_PAYMENT,
            reference_id=self.payment.payment_no,
        )
        self.assertEqual(credit.amount, Decimal('30.00'))
        self.assertEqual(spend.amount, Decimal('-30.00'))
        self.assertEqual(credit.balance_after, Decimal('80.00'))
        self.assertEqual(spend.balance_after, Decimal('50.00'))
        self.assertEqual(self.payment.notify_payload['diamond_settlement']['diamonds'], 300)

    def test_regular_refund_returns_diamonds_for_wechat_direct_payment(self):
        refund = create_wallet_refund(
            self.payment.payment_no,
            Decimal('30.00'),
            reason='普通退钻石',
            operator=self.user,
        )

        self.wallet.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(refund.status, Refund.STATUS_SUCCEEDED)
        self.assertEqual(self.wallet.balance, Decimal('80.00'))
        self.assertEqual(self.payment.status, 'refunded')
        refund_entry = ClientWalletLedger.objects.get(
            entry_type=ClientWalletLedger.TYPE_REFUND_IN,
            reference_id=refund.refund_no,
        )
        self.assertEqual(refund_entry.amount, Decimal('30.00'))

    @patch('apps.payments.wechat_virtual_refunds.xpay_post')
    def test_direct_wechat_original_refund_never_credits_wallet(self, mocked_xpay_post):
        mocked_xpay_post.side_effect = [
            {'errcode': 0, 'order': {'left_fee': 3000}},
            {'errcode': 0, 'order': {'left_fee': 3000}},
            {
                'errcode': 0,
                'refund_order_id': 'REF_REMOTE_001',
                'refund_wx_order_id': 'WX_REFUND_001',
            },
        ]

        refund = refund_payment_to_wechat_original(self.payment, operator=self.user)
        self.wallet.refresh_from_db()
        self.assertEqual(refund.status, Refund.STATUS_PROCESSING)
        self.assertEqual(cash_refund_status(refund), 'processing')
        self.assertEqual(self.wallet.balance, Decimal('50.00'))
        self.assertFalse(
            ClientWalletLedger.objects.filter(entry_type=ClientWalletLedger.TYPE_REFUND_IN).exists()
        )

        mocked_xpay_post.side_effect = None
        mocked_xpay_post.return_value = {
            'errcode': 0,
            'order': {
                'order_type': 1,
                'status': 5,
                'refund_fee': 3000,
                'wx_order_id': 'WX_REFUND_001',
            },
        }
        with patch('apps.accounts.vip.record_successful_refund'), patch(
            'apps.earnings.services.reverse_refund_earnings'
        ):
            refund = sync_wechat_original_refund(refund, operator=self.user)

        refund.refresh_from_db()
        self.payment.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(refund.status, Refund.STATUS_SUCCEEDED)
        self.assertEqual(cash_refund_status(refund), 'succeeded')
        self.assertEqual(self.payment.status, 'refunded')
        self.assertEqual(self.wallet.balance, Decimal('50.00'))
        self.assertFalse(
            ClientWalletLedger.objects.filter(entry_type=ClientWalletLedger.TYPE_REFUND_IN).exists()
        )

    @patch('apps.payments.wechat_virtual_refunds.xpay_post')
    def test_existing_diamond_refund_can_convert_to_wechat_and_restore_on_failure(self, mocked_xpay_post):
        wallet_refund = create_wallet_refund(
            self.payment.payment_no,
            Decimal('30.00'),
            reason='先退钻石',
            operator=self.user,
        )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('80.00'))

        mocked_xpay_post.side_effect = [
            {'errcode': 0, 'order': {'left_fee': 3000}},
            {'errcode': 0, 'order': {'left_fee': 3000}},
            {
                'errcode': 0,
                'refund_order_id': wallet_refund.refund_no,
                'refund_wx_order_id': 'WX_REFUND_CONVERT_001',
            },
        ]
        converted = refund_payment_to_wechat_original(self.payment, operator=self.user)
        self.wallet.refresh_from_db()
        self.assertEqual(converted.pk, wallet_refund.pk)
        self.assertEqual(cash_refund_status(converted), 'processing')
        self.assertEqual(self.wallet.balance, Decimal('50.00'))
        self.assertTrue(
            ClientWalletLedger.objects.filter(
                entry_type=ClientWalletLedger.TYPE_WECHAT_REFUND_CONVERSION,
                reference_id=f'{wallet_refund.refund_no}:wechat-original',
            ).exists()
        )

        mocked_xpay_post.side_effect = None
        mocked_xpay_post.return_value = {
            'errcode': 0,
            'order': {
                'order_type': 1,
                'status': 7,
                'refund_fee': 3000,
            },
        }
        converted = sync_wechat_original_refund(converted, operator=self.user)

        converted.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(converted.status, Refund.STATUS_SUCCEEDED)
        self.assertEqual(cash_refund_status(converted), 'failed')
        self.assertEqual(self.wallet.balance, Decimal('80.00'))
        self.assertTrue(
            ClientWalletLedger.objects.filter(
                entry_type=ClientWalletLedger.TYPE_WECHAT_REFUND_RESTORE,
                reference_id=f'{wallet_refund.refund_no}:wechat-original:restore',
            ).exists()
        )
