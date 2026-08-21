from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package, PlayerType
from apps.orders.models import Order, OrderDesignation
from apps.players.models import Player

from .coin_balance_service import pay_order_with_coin_aware_balance
from .models import ClientWallet, ClientWalletLedger, RechargeOrder


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
class TargetedCoinCheckoutTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_user(username='targeted-coin-boss')
        self.profile = ClientProfile.objects.create(
            user=self.boss,
            openid='openid_targeted_coin_boss',
            nickname='指定商品Coin测试老板',
        )
        self.player_user = User.objects.create_user(username='targeted-coin-player')
        self.player_type = PlayerType.objects.create(name='指定Coin测试陪玩类型', priority=1)
        self.player = Player.objects.create(
            user=self.player_user,
            name='指定Coin测试陪玩',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
            is_online=True,
        )
        self.package = Package.objects.create(
            name='指定陪玩专属Coin商品',
            selling_mode=Package.SELLING_MODE_PLAYER_DESIGNATED,
            owner_player=self.player,
            player_count=1,
            base_price=20,
        )
        self.order = Order.objects.create(
            order_no='TARGETCOINORDER001',
            boss_user=self.boss,
            boss_wechat='targeted-coin-boss',
            package=self.package,
            package_name_snapshot=self.package.name,
            required_players=1,
            total_price_per_hour=20,
            total_amount=20,
            status=Order.STATUS_PENDING_PAYMENT,
            fulfillment_mode=Order.FULFILLMENT_MODE_TARGETED,
            target_player=self.player,
            target_player_name_snapshot=self.player.name,
        )
        self.wallet = ClientWallet.objects.create(
            profile=self.profile,
            balance=Decimal('20.00'),
            recharged_total=Decimal('20.00'),
        )
        self.recharge = RechargeOrder.objects.create(
            recharge_no='RCGTARGETCOIN001',
            profile=self.profile,
            product=None,
            amount=Decimal('20.00'),
            channel=RechargeOrder.CHANNEL_WECHAT_VIRTUAL,
            status=RechargeOrder.STATUS_CREDITED,
            notify_payload={
                'mode': 'short_series_coin',
                'client_platform': 'ios',
                'wechat_coin_units_per_yuan': 100,
            },
        )
        ClientWalletLedger.objects.create(
            wallet=self.wallet,
            entry_type=ClientWalletLedger.TYPE_RECHARGE,
            amount=Decimal('20.00'),
            balance_after=Decimal('20.00'),
            reference_type='recharge_order',
            reference_id=self.recharge.recharge_no,
        )

    def test_targeted_product_uses_coin_and_creates_designation_after_payment(self):
        with patch(
            'apps.wallet.coin_sync.exchange_code_for_session',
            return_value=(self.profile.openid, 'targeted-session'),
        ), patch(
            'apps.wallet.coin_sync.user_xpay_post',
            side_effect=[
                {'errcode': 0, 'balance': 2000},
                {'errcode': 0, 'balance': 0},
            ],
        ) as mocked_xpay:
            result = pay_order_with_coin_aware_balance(
                self.order.order_no,
                self.boss,
                code='fresh-targeted-code',
                user_ip='1.2.3.4',
            )

        self.assertEqual(result['status'], 'paid')
        self.assertEqual(result['wechat_coin_diamonds'], '200.0')
        self.assertEqual(result['wechat_coin_units'], 2000)
        self.assertEqual(
            [call.args[0] for call in mocked_xpay.call_args_list],
            ['/xpay/query_user_balance', '/xpay/currency_pay'],
        )

        self.order.refresh_from_db()
        self.assertTrue(self.order.paid)
        self.assertEqual(self.order.status, Order.STATUS_WAITING)
        self.assertEqual(self.order.payment_method, 'balance')

        designation = OrderDesignation.objects.get(order=self.order, player=self.player)
        self.assertEqual(designation.status, OrderDesignation.STATUS_PENDING)
        self.assertIsNotNone(designation.expires_at)

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('0.00'))
