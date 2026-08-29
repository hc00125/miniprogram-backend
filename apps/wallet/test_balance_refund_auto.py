from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from rest_framework.exceptions import ValidationError

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order
from apps.orders.targeted_refund_fixes import cancel_targeted_order
from apps.payments.models import Payment, Refund
from apps.payments.refund_integrity import ensure_payment_refund, settle_balance_refund
from apps.payments.services import create_refund

from .models import ClientWallet, ClientWalletLedger
from .services import pay_order_with_balance


class AutomaticBalanceRefundTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='auto-refund-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='openid_auto_refund_boss',
            nickname='自动余额退款老板',
        )
        self.package = Package.objects.create(
            name='自动余额退款套餐',
            player_count=1,
            base_price=30,
        )
        self.order = Order.objects.create(
            order_no='AUTO_BAL_REF_001',
            boss_user=self.user,
            boss_wechat=self.profile.openid,
            package=self.package,
            required_players=1,
            total_price_per_hour=30,
            total_amount=30,
            status=Order.STATUS_PENDING_PAYMENT,
        )
        self.wallet = ClientWallet.objects.create(
            profile=self.profile,
            balance=Decimal('100.00'),
            recharged_total=Decimal('100.00'),
        )
        result = pay_order_with_balance(self.order.order_no, self.user)
        self.payment = Payment.objects.get(payment_no=result['payment_no'])

    def test_create_full_balance_refund_immediately_returns_wallet(self):
        refund = create_refund(
            self.payment.payment_no,
            Decimal('30.00'),
            reason='订单取消全额退款',
            operator=self.user,
        )

        refund.refresh_from_db()
        self.payment.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(refund.status, Refund.STATUS_SUCCEEDED)
        self.assertEqual(self.payment.status, 'refunded')
        self.assertEqual(self.wallet.balance, Decimal('100.00'))
        entries = ClientWalletLedger.objects.filter(
            entry_type=ClientWalletLedger.TYPE_REFUND_IN,
            reference_id=refund.refund_no,
        )
        self.assertEqual(entries.count(), 1)
        self.assertEqual(entries.get().amount, Decimal('30.00'))

    def test_partial_balance_refunds_settle_and_full_total_marks_payment_refunded(self):
        first = create_refund(self.payment.payment_no, Decimal('10.00'), reason='部分退款一')
        self.payment.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(first.status, Refund.STATUS_SUCCEEDED)
        self.assertEqual(self.payment.status, 'paid')
        self.assertEqual(self.wallet.balance, Decimal('80.00'))

        second = create_refund(self.payment.payment_no, Decimal('20.00'), reason='部分退款二')
        self.payment.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(second.status, Refund.STATUS_SUCCEEDED)
        self.assertEqual(self.payment.status, 'refunded')
        self.assertEqual(self.wallet.balance, Decimal('100.00'))
        self.assertEqual(
            ClientWalletLedger.objects.filter(
                entry_type=ClientWalletLedger.TYPE_REFUND_IN,
            ).count(),
            2,
        )

    def test_repeated_settlement_never_credits_twice(self):
        refund = create_refund(self.payment.payment_no, Decimal('30.00'), reason='幂等退款')
        settle_balance_refund(refund.pk)
        settle_balance_refund(refund.pk)

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('100.00'))
        self.assertEqual(
            ClientWalletLedger.objects.filter(
                entry_type=ClientWalletLedger.TYPE_REFUND_IN,
                reference_id=refund.refund_no,
            ).count(),
            1,
        )

    def test_existing_pending_balance_refund_is_settled_and_reused(self):
        pending = Refund.objects.create(
            refund_no='PENDING_BAL_REF_001',
            payment=self.payment,
            order=self.order,
            amount=Decimal('30.00'),
            reason='旧版本遗留待处理退款',
            status=Refund.STATUS_PENDING,
        )

        refund = ensure_payment_refund(
            self.payment.payment_no,
            Decimal('30.00'),
            reason='指定订单取消退款',
            operator=self.user,
        )

        pending.refresh_from_db()
        self.wallet.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(refund.pk, pending.pk)
        self.assertEqual(pending.status, Refund.STATUS_SUCCEEDED)
        self.assertEqual(self.wallet.balance, Decimal('100.00'))
        self.assertEqual(self.payment.status, 'refunded')
        self.assertEqual(Refund.objects.filter(payment=self.payment).count(), 1)
        self.assertEqual(
            ClientWalletLedger.objects.filter(
                entry_type=ClientWalletLedger.TYPE_REFUND_IN,
                reference_id=pending.refund_no,
            ).count(),
            1,
        )

    def test_targeted_cancel_refunds_wallet_before_committing_cancel(self):
        Order.objects.filter(pk=self.order.pk).update(
            fulfillment_mode=Order.FULFILLMENT_MODE_TARGETED,
        )
        self.order.refresh_from_db()

        refund = cancel_targeted_order(self.order, '指定陪玩师邀请超时', operator=self.user)

        self.order.refresh_from_db()
        self.wallet.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_CANCELLED)
        self.assertIsNotNone(refund)
        self.assertEqual(refund.status, Refund.STATUS_SUCCEEDED)
        self.assertEqual(self.wallet.balance, Decimal('100.00'))
        self.assertEqual(self.payment.status, 'refunded')

    def test_targeted_cancel_rolls_back_when_paid_payment_is_missing(self):
        Order.objects.filter(pk=self.order.pk).update(
            fulfillment_mode=Order.FULFILLMENT_MODE_TARGETED,
        )
        self.payment.delete()
        self.order.refresh_from_db()
        before_status = self.order.status

        with self.assertRaises(ValidationError):
            cancel_targeted_order(self.order, '指定陪玩师邀请超时', operator=self.user)

        self.order.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(self.order.status, before_status)
        self.assertNotEqual(self.order.status, Order.STATUS_CANCELLED)
        self.assertEqual(self.wallet.balance, Decimal('70.00'))

    def test_wechat_refund_auto_settles_to_wallet(self):
        wechat_order = Order.objects.create(
            order_no='AUTO_WX_REF_001',
            boss_user=self.user,
            boss_wechat=self.profile.openid,
            package=self.package,
            required_players=1,
            total_price_per_hour=30,
            total_amount=30,
            status=Order.STATUS_CANCELLED,
            paid=True,
            payment_method='wechat',
        )
        wechat_payment = Payment.objects.create(
            payment_no='AUTO_WX_PAY_001',
            order=wechat_order,
            channel='wechat',
            scene='jsapi',
            amount=30,
            status='paid',
        )

        refund = create_refund(wechat_payment.payment_no, Decimal('30.00'), reason='微信异步退款')

        refund.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(refund.status, Refund.STATUS_SUCCEEDED)
        self.assertEqual(self.wallet.balance, Decimal('100.00'))
        self.assertTrue(
            ClientWalletLedger.objects.filter(reference_id=refund.refund_no).exists()
        )

    def test_direct_pending_record_stays_pending_until_explicit_settlement(self):
        refund = Refund.objects.create(
            refund_no='DIRECT_BAL_REF_001',
            payment=self.payment,
            order=self.order,
            amount=30,
            reason='后台直接创建退款记录',
            status=Refund.STATUS_PENDING,
        )

        refund.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(refund.status, Refund.STATUS_PENDING)
        self.assertEqual(self.wallet.balance, Decimal('70.00'))

        settle_balance_refund(refund.pk)
        refund.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(refund.status, Refund.STATUS_SUCCEEDED)
        self.assertEqual(self.wallet.balance, Decimal('100.00'))

    def test_reconcile_command_repairs_existing_pending_balance_refund(self):
        refund = Refund.objects.create(
            refund_no='HISTORY_BAL_REF_001',
            payment=self.payment,
            order=self.order,
            amount=30,
            reason='历史待处理退款',
            status=Refund.STATUS_FAILED,
        )
        Refund.objects.filter(pk=refund.pk).update(status=Refund.STATUS_PENDING)

        output = StringIO()
        call_command(
            'reconcile_balance_refunds',
            '--apply',
            '--order-no',
            self.order.order_no,
            stdout=output,
        )

        refund.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(refund.status, Refund.STATUS_SUCCEEDED)
        self.assertEqual(self.wallet.balance, Decimal('100.00'))
        self.assertIn('已入账', output.getvalue())

    def test_reconcile_command_can_create_missing_refund_for_cancelled_order(self):
        self.order.status = Order.STATUS_CANCELLED
        self.order.save(update_fields=['status'])

        output = StringIO()
        call_command(
            'reconcile_balance_refunds',
            '--apply',
            '--create-missing',
            '--order-no',
            self.order.order_no,
            stdout=output,
        )

        refund = Refund.objects.get(payment=self.payment)
        self.wallet.refresh_from_db()
        self.assertEqual(refund.status, Refund.STATUS_SUCCEEDED)
        self.assertEqual(refund.amount, Decimal('30.00'))
        self.assertEqual(self.wallet.balance, Decimal('100.00'))
        self.assertIn('已创建并入账', output.getvalue())
