from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import ClientProfile

from .ios_credit import decorate_wallet_recharge_payload
from .models import ClientWallet, ClientWalletLedger, RechargeOrder
from .services import mark_recharge_paid


class IOSRechargeCreditTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='ios-recharge-user')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='ios-recharge-openid',
            nickname='iOS充值测试用户',
        )

    def create_recharge(self, *, checkout_order_no=''):
        payload = {
            'recharge': True,
            'mode': 'short_series_coin',
            'client_platform': 'ios',
            'platform_fee_percent': '15.00',
            'estimated_platform_fee_yuan': '15.00',
            'estimated_settlement_yuan': '85.00',
            'checkout_order_no': checkout_order_no,
            'wechat_coin_units_per_yuan': 10,
        }
        return RechargeOrder.objects.create(
            recharge_no=f'IOS-{checkout_order_no or "WALLET"}',
            profile=self.profile,
            product=None,
            amount=Decimal('100.00'),
            channel=RechargeOrder.CHANNEL_MOCK,
            status=RechargeOrder.STATUS_PAYING,
            checkout_order_no=checkout_order_no,
            notify_payload=payload,
        )

    def test_standalone_ios_recharge_credits_net_diamonds(self):
        recharge = self.create_recharge()
        recharge = mark_recharge_paid(recharge, 'mock-ios', dict(recharge.notify_payload))

        recharge.refresh_from_db()
        wallet = ClientWallet.objects.get(profile=self.profile)
        ledger = ClientWalletLedger.objects.get(
            wallet=wallet,
            entry_type=ClientWalletLedger.TYPE_RECHARGE,
            reference_id=recharge.recharge_no,
        )

        self.assertEqual(wallet.balance, Decimal('85.00'))
        self.assertEqual(wallet.recharged_total, Decimal('85.00'))
        self.assertEqual(ledger.amount, Decimal('85.00'))
        self.assertEqual(ledger.balance_after, Decimal('85.00'))
        self.assertTrue(recharge.notify_payload['fee_deducted_from_credit'])
        self.assertEqual(recharge.notify_payload['charged_platform_fee_percent'], '15.00')
        self.assertEqual(recharge.notify_payload['platform_fee_percent'], '0.00')
        self.assertEqual(recharge.notify_payload['credited_amount_yuan'], '85.00')

        payload = decorate_wallet_recharge_payload({}, recharge)
        self.assertEqual(Decimal(str(payload['diamonds'])), Decimal('850'))

    def test_order_linked_ios_coin_checkout_keeps_full_credit(self):
        recharge = self.create_recharge(checkout_order_no='ORDER-1')
        recharge = mark_recharge_paid(recharge, 'mock-ios-order', dict(recharge.notify_payload))

        recharge.refresh_from_db()
        wallet = ClientWallet.objects.get(profile=self.profile)
        ledger = ClientWalletLedger.objects.get(
            wallet=wallet,
            entry_type=ClientWalletLedger.TYPE_RECHARGE,
            reference_id=recharge.recharge_no,
        )

        self.assertEqual(wallet.balance, Decimal('100.00'))
        self.assertEqual(wallet.recharged_total, Decimal('100.00'))
        self.assertEqual(ledger.amount, Decimal('100.00'))
        self.assertFalse(recharge.notify_payload.get('fee_deducted_from_credit', False))
        self.assertEqual(recharge.notify_payload['platform_fee_percent'], '15.00')
