from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.catalog.models import Package, PlayerType
from apps.orders.models import Order, OrderPlayer
from apps.players.models import Player

from .models import (
    OrderCommissionOverride,
    PlayerEarning,
    PlayerWallet,
    Withdrawal,
)
from .services import (
    approve_withdrawal,
    create_order_earnings,
    create_withdrawal,
    mark_withdrawal_paid,
    recalculate_pending_order_earnings,
    release_due_earnings,
    return_withdrawal,
)


class EarningsSettlementTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='earnings-owner')
        self.player_type = PlayerType.objects.create(name='收益测试陪玩', priority=1)
        self.package = Package.objects.create(name='收益测试套餐', player_count=2, base_price=100)
        self.player_users = [
            User.objects.create_user(username='earnings-player-1'),
            User.objects.create_user(username='earnings-player-2'),
        ]
        self.players = [
            Player.objects.create(
                user=self.player_users[index],
                name=f'收益陪玩{index + 1}',
                player_type=self.player_type,
                status=Player.STATUS_APPROVED,
            )
            for index in range(2)
        ]
        self.order = Order.objects.create(
            order_no='EARNROOT001',
            boss_user=self.owner,
            boss_wechat='earnings-boss',
            package=self.package,
            package_name_snapshot='收益测试套餐',
            required_players=2,
            total_price_per_hour=100,
            total_amount=100,
            status=Order.STATUS_IN_PROGRESS,
            paid=True,
            order_type=Order.ORDER_TYPE_NORMAL,
        )
        for player in self.players:
            OrderPlayer.objects.create(order=self.order, player=player)

    def complete_order(self):
        self.order.status = Order.STATUS_COMPLETED
        self.order.end_time = timezone.now()
        self.order.save(update_fields=['status', 'end_time'])
        self.order.refresh_from_db()

    def test_completion_creates_pending_earnings_with_default_15_percent(self):
        self.complete_order()

        earnings = list(PlayerEarning.objects.filter(order=self.order).order_by('player_id'))
        self.assertEqual(len(earnings), 2)
        for earning in earnings:
            self.assertEqual(earning.gross_amount, Decimal('50.00'))
            self.assertEqual(earning.commission_rate, Decimal('15.00'))
            self.assertEqual(earning.commission_amount, Decimal('7.50'))
            self.assertEqual(earning.net_amount, Decimal('42.50'))
            self.assertEqual(earning.status, PlayerEarning.STATUS_PENDING)
            wallet = PlayerWallet.objects.get(player=earning.player)
            self.assertEqual(wallet.pending_balance, Decimal('42.50'))
            self.assertEqual(wallet.available_balance, Decimal('0.00'))

    def test_paid_renewal_amount_is_included_in_wages(self):
        Order.objects.create(
            order_no='EARNRENEW01',
            boss_user=self.owner,
            boss_wechat='earnings-boss',
            package=self.package,
            required_players=2,
            total_price_per_hour=20,
            total_amount=20,
            status=Order.STATUS_COMPLETED,
            paid=True,
            order_type=Order.ORDER_TYPE_RENEWAL,
            parent_order=self.order,
            renewal_index=1,
        )
        self.complete_order()

        earnings = list(PlayerEarning.objects.filter(order=self.order))
        self.assertEqual(len(earnings), 2)
        for earning in earnings:
            self.assertEqual(earning.gross_amount, Decimal('60.00'))
            self.assertEqual(earning.commission_amount, Decimal('9.00'))
            self.assertEqual(earning.net_amount, Decimal('51.00'))

    def test_order_override_can_set_zero_commission(self):
        OrderCommissionOverride.objects.create(
            order=self.order,
            commission_rate=Decimal('0.00'),
            reason='奖励单不抽成',
            created_by=self.owner,
        )
        self.complete_order()

        for earning in PlayerEarning.objects.filter(order=self.order):
            self.assertEqual(earning.commission_amount, Decimal('0.00'))
            self.assertEqual(earning.net_amount, Decimal('50.00'))

    def test_pending_commission_can_be_recalculated(self):
        self.complete_order()
        override = OrderCommissionOverride.objects.create(
            order=self.order,
            commission_rate=Decimal('10.00'),
            reason='活动单少抽成',
            created_by=self.owner,
        )
        recalculate_pending_order_earnings(override.order, operator=self.owner)

        for earning in PlayerEarning.objects.filter(order=self.order):
            earning.refresh_from_db()
            self.assertEqual(earning.commission_rate, Decimal('10.00'))
            self.assertEqual(earning.net_amount, Decimal('45.00'))
            self.assertEqual(earning.player.wallet.pending_balance, Decimal('45.00'))

    def test_creation_is_idempotent(self):
        self.complete_order()
        create_order_earnings(self.order)
        create_order_earnings(self.order)
        self.assertEqual(PlayerEarning.objects.filter(order=self.order).count(), 2)


class WalletWithdrawalTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='wallet-owner')
        self.finance = User.objects.create_user(username='wallet-finance', is_staff=True)
        self.player_user = User.objects.create_user(username='wallet-player')
        self.player_type = PlayerType.objects.create(name='钱包测试陪玩', priority=1)
        self.package = Package.objects.create(name='钱包测试套餐', player_count=1, base_price=100)
        self.player = Player.objects.create(
            user=self.player_user,
            name='钱包陪玩',
            player_type=self.player_type,
            status=Player.STATUS_APPROVED,
        )
        self.order = Order.objects.create(
            order_no='WALLETROOT1',
            boss_user=self.owner,
            boss_wechat='wallet-boss',
            package=self.package,
            required_players=1,
            total_price_per_hour=100,
            total_amount=100,
            status=Order.STATUS_IN_PROGRESS,
            paid=True,
        )
        OrderPlayer.objects.create(order=self.order, player=self.player)
        self.order.status = Order.STATUS_COMPLETED
        self.order.end_time = timezone.now()
        self.order.save(update_fields=['status', 'end_time'])
        self.earning = PlayerEarning.objects.get(order=self.order, player=self.player)

    def release_wage(self):
        self.earning.review_until = timezone.now() - timedelta(seconds=1)
        self.earning.save(update_fields=['review_until'])
        release_due_earnings(player=self.player)
        self.earning.refresh_from_db()

    def test_eight_day_review_release_moves_balance_to_available(self):
        self.release_wage()
        wallet = PlayerWallet.objects.get(player=self.player)
        self.assertEqual(self.earning.status, PlayerEarning.STATUS_AVAILABLE)
        self.assertEqual(wallet.pending_balance, Decimal('0.00'))
        self.assertEqual(wallet.available_balance, Decimal('85.00'))
        self.assertEqual(self.earning.available_amount, Decimal('85.00'))

    def test_withdrawal_freezes_then_finance_marks_paid(self):
        self.release_wage()
        withdrawal = create_withdrawal(
            self.player,
            Decimal('50.00'),
            Withdrawal.METHOD_WECHAT,
            '测试收款人',
            'wx-test-account',
        )
        wallet = PlayerWallet.objects.get(player=self.player)
        self.assertEqual(wallet.available_balance, Decimal('35.00'))
        self.assertEqual(wallet.withdrawing_balance, Decimal('50.00'))

        approve_withdrawal(withdrawal, self.finance)
        withdrawal.refresh_from_db()
        withdrawal.transfer_no = 'FINANCE-001'
        withdrawal.save(update_fields=['transfer_no'])
        mark_withdrawal_paid(withdrawal, self.finance)

        wallet.refresh_from_db()
        self.earning.refresh_from_db()
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, Withdrawal.STATUS_PAID)
        self.assertEqual(wallet.withdrawing_balance, Decimal('0.00'))
        self.assertEqual(wallet.withdrawn_total, Decimal('50.00'))
        self.assertEqual(self.earning.available_amount, Decimal('35.00'))
        self.assertEqual(self.earning.withdrawn_amount, Decimal('50.00'))

    def test_rejected_withdrawal_returns_available_balance(self):
        self.release_wage()
        withdrawal = create_withdrawal(
            self.player,
            Decimal('40.00'),
            Withdrawal.METHOD_BANK,
            '测试收款人',
            '6222000000000000',
        )
        return_withdrawal(withdrawal, '收款信息不正确', self.finance)

        wallet = PlayerWallet.objects.get(player=self.player)
        self.earning.refresh_from_db()
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, Withdrawal.STATUS_REJECTED)
        self.assertEqual(wallet.available_balance, Decimal('85.00'))
        self.assertEqual(wallet.withdrawing_balance, Decimal('0.00'))
        self.assertEqual(self.earning.available_amount, Decimal('85.00'))
        self.assertEqual(self.earning.withdrawing_amount, Decimal('0.00'))
