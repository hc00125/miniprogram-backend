from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from apps.catalog.models import PlayerType
from apps.players.models import Player

from .models import PlayerWallet, Withdrawal
from .payout_batch_models import WithdrawalPayoutBatch, WithdrawalPayoutBatchItem
from .payout_batches import approve_withdrawals_batch, mark_withdrawals_paid_batch
from .withdrawals import create_withdrawal


class WithdrawalPayoutBatchTests(TestCase):
    def setUp(self):
        self.finance = User.objects.create_user(
            username='batch-finance',
            password='test-password',
            is_staff=True,
        )
        player_type = PlayerType.objects.create(name='批量付款测试陪玩', priority=1)
        self.player = Player.objects.create(
            name='批量付款陪玩',
            player_type=player_type,
            status=Player.STATUS_APPROVED,
        )
        self.wallet, _ = PlayerWallet.objects.get_or_create(player=self.player)
        self.wallet.available_balance = Decimal('500.00')
        self.wallet.save(update_fields=['available_balance'])

    def create_two_withdrawals(self):
        first = create_withdrawal(
            self.player,
            Decimal('80.00'),
            Withdrawal.METHOD_WECHAT,
            '测试收款人',
            'wx-batch-account',
        )
        second = create_withdrawal(
            self.player,
            Decimal('120.00'),
            Withdrawal.METHOD_WECHAT,
            '测试收款人',
            'wx-batch-account',
        )
        return first, second

    def test_batch_approval_accepts_optional_common_note(self):
        first, second = self.create_two_withdrawals()

        approved = approve_withdrawals_batch(
            [first.id, second.id],
            self.finance,
            admin_note='本周工资统一审核',
        )

        self.assertEqual(len(approved), 2)
        for withdrawal in (first, second):
            withdrawal.refresh_from_db()
            self.assertEqual(withdrawal.status, Withdrawal.STATUS_PENDING_PAYMENT)
            self.assertEqual(withdrawal.reviewed_by, self.finance)
            self.assertIn('本周工资统一审核', withdrawal.admin_note)

    def test_batch_payment_succeeds_without_external_transfer_number(self):
        first, second = self.create_two_withdrawals()
        approve_withdrawals_batch([first.id, second.id], self.finance)

        batch = mark_withdrawals_paid_batch(
            [first.id, second.id],
            operator=self.finance,
            payment_method=Withdrawal.METHOD_WECHAT,
            external_transfer_no='',
            admin_note='',
            proof_url='',
        )

        self.assertTrue(batch.batch_no.startswith('PAYOUT'))
        self.assertEqual(batch.item_count, 2)
        self.assertEqual(batch.total_amount, Decimal('200.00'))
        self.assertEqual(batch.external_transfer_no, '')
        self.assertEqual(WithdrawalPayoutBatch.objects.count(), 1)
        self.assertEqual(WithdrawalPayoutBatchItem.objects.filter(batch=batch).count(), 2)

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal('300.00'))
        self.assertEqual(self.wallet.withdrawing_balance, Decimal('0.00'))
        self.assertEqual(self.wallet.withdrawn_total, Decimal('200.00'))

        for withdrawal in (first, second):
            withdrawal.refresh_from_db()
            self.assertEqual(withdrawal.status, Withdrawal.STATUS_PAID)
            self.assertEqual(withdrawal.transfer_no, '')
            self.assertEqual(withdrawal.paid_by, self.finance)
            self.assertEqual(withdrawal.payout_batch_item.batch_id, batch.id)

    def test_batch_payment_copies_optional_public_payment_information(self):
        first, second = self.create_two_withdrawals()
        approve_withdrawals_batch([first.id, second.id], self.finance)

        batch = mark_withdrawals_paid_batch(
            [first.id, second.id],
            operator=self.finance,
            payment_method=Withdrawal.METHOD_ALIPAY,
            external_transfer_no='ALI-BATCH-20260804',
            admin_note='支付宝批量付款',
            proof_url='https://example.com/proof.png',
        )

        self.assertEqual(batch.payment_method, Withdrawal.METHOD_ALIPAY)
        self.assertEqual(batch.external_transfer_no, 'ALI-BATCH-20260804')
        for withdrawal in (first, second):
            withdrawal.refresh_from_db()
            self.assertEqual(withdrawal.payment_method, Withdrawal.METHOD_ALIPAY)
            self.assertEqual(withdrawal.transfer_no, 'ALI-BATCH-20260804')
            self.assertEqual(withdrawal.proof_url, 'https://example.com/proof.png')
            self.assertIn('支付宝批量付款', withdrawal.admin_note)
