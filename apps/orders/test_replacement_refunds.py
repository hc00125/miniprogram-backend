from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.payments.models import Payment, Refund
from apps.wallet.models import ClientWallet

from .cancellation_models import OrderReplacementState
from .models import Order
from .replacement_refunds import cancel_remaining_and_refund


class ReplacementRemainingRefundTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='refund-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='refund-boss-openid',
            nickname='退款测试老板',
        )
        self.wallet, _ = ClientWallet.objects.get_or_create(profile=self.profile)
        self.package = Package.objects.create(
            name='指定陪玩退款测试套餐',
            base_price=25,
            player_count=1,
            is_active=True,
        )

    def make_root(self, order_no, *, hours=1, amount=25, status=Order.STATUS_READY_TO_START):
        order = Order.objects.create(
            order_no=order_no,
            boss_user=self.user,
            boss_wechat=self.profile.openid,
            package=self.package,
            required_players=1,
            booked_hours=hours,
            total_price_per_hour=25,
            total_amount=amount,
            status=status,
            paid=True,
            fulfillment_mode=Order.FULFILLMENT_MODE_TARGETED,
        )
        Payment.objects.create(
            payment_no=f'PAY-{order_no}',
            order=order,
            channel='wechat_virtual',
            scene='mini_program',
            amount=Decimal(str(amount)),
            status='paid',
        )
        return order

    def make_state(self, order, *, remaining_minutes, status=OrderReplacementState.STATUS_OPEN):
        return OrderReplacementState.objects.create(
            order=order,
            mode=OrderReplacementState.MODE_TARGETED,
            status=status,
            missing_slots=1,
            resume_status=order.status,
            remaining_minutes=remaining_minutes,
            cancelled_player_name='退出陪玩',
        )

    def test_pre_service_cancel_refunds_full_payment_and_resolves_replacement(self):
        order = self.make_root('RPLCANCEL000000001', hours=1, amount=25)
        state = self.make_state(order, remaining_minutes=60)

        result = cancel_remaining_and_refund(order, self.user)

        order.refresh_from_db()
        state.refresh_from_db()
        self.wallet.refresh_from_db()
        refund = Refund.objects.get(order=order)

        self.assertEqual(result['refund_amount'], Decimal('25.00'))
        self.assertEqual(result['remaining_minutes'], 60)
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        self.assertEqual(state.status, OrderReplacementState.STATUS_RESOLVED)
        self.assertEqual(state.missing_slots, 0)
        self.assertEqual(refund.status, Refund.STATUS_SUCCEEDED)
        self.assertEqual(refund.amount, Decimal('25.00'))
        self.assertEqual(self.wallet.balance, Decimal('25.00'))

    def test_group_refund_uses_remaining_minutes_across_root_and_paid_renewal(self):
        root = self.make_root(
            'RPLCANCEL000000002',
            hours=1,
            amount=25,
            status=Order.STATUS_IN_PROGRESS,
        )
        renewal = Order.objects.create(
            order_no='RPLCANCEL000000003',
            boss_user=self.user,
            boss_wechat=self.profile.openid,
            package=self.package,
            required_players=1,
            booked_hours=1,
            total_price_per_hour=25,
            total_amount=25,
            status=Order.STATUS_COMPLETED,
            paid=True,
            order_type=Order.ORDER_TYPE_RENEWAL,
            parent_order=root,
            renewal_index=1,
        )
        Payment.objects.create(
            payment_no='PAY-RPLCANCEL000000003',
            order=renewal,
            channel='balance',
            scene='balance',
            amount=Decimal('25.00'),
            status='paid',
        )
        self.make_state(root, remaining_minutes=90)

        result = cancel_remaining_and_refund(root, self.user)

        root.refresh_from_db()
        self.wallet.refresh_from_db()
        root_refund = Refund.objects.get(order=root)
        renewal_refund = Refund.objects.get(order=renewal)

        # 90 minutes remain from a two-hour group: all 60 renewal minutes plus
        # the final 30 minutes of the original hour.
        self.assertEqual(result['refund_amount'], Decimal('37.50'))
        self.assertEqual(root_refund.amount, Decimal('12.50'))
        self.assertEqual(renewal_refund.amount, Decimal('25.00'))
        self.assertEqual(self.wallet.balance, Decimal('37.50'))
        self.assertEqual(root.status, Order.STATUS_CANCELLED)

    def test_legacy_cancel_requested_can_finish_without_customer_service(self):
        order = self.make_root('RPLCANCEL000000004', hours=1, amount=25)
        state = self.make_state(
            order,
            remaining_minutes=60,
            status=OrderReplacementState.STATUS_CANCEL_REQUESTED,
        )

        result = cancel_remaining_and_refund(order, self.user)

        state.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(result['refund_amount'], Decimal('25.00'))
        self.assertEqual(state.status, OrderReplacementState.STATUS_RESOLVED)
        self.assertEqual(self.wallet.balance, Decimal('25.00'))
