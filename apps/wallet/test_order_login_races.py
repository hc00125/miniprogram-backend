"""Parent regression probes for authentication/dispatch interleavings."""
from unittest.mock import patch
from django.test import TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError
from apps.orders.models import Order
from . import test_order_durable as fixtures
from .test_order_durable import RecordingAdapter
from .coin_balance_service import pay_order_with_coin_aware_balance as pay
from .spend_models import WalletSpendAttempt
from .models import ClientWalletLedger


@override_settings(WECHAT_VIRTUALPAY_ENV=1, WECHAT_VIRTUALPAY_ENABLED=True,
                   SHARED_SPEND_PLATFORM_APPROVED=True, WECHAT_VIRTUALPAY_COIN_UNITS_PER_YUAN=10)
class OrderLoginRaceTests(TransactionTestCase):
    def setUp(self):
        fixtures.OriginalOrderDurableTests.setUp(self)

    def test_price_changed_while_logging_in_is_not_silently_charged(self):
        adapter = RecordingAdapter(self)
        def change_price(context, code):
            Order.objects.filter(pk=self.order.pk).update(total_amount=7)
            return 'ephemeral'
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter), \
             patch.object(adapter, 'authenticate', side_effect=change_price):
            with self.assertRaises(ValidationError) as caught:
                pay(self.order.order_no, self.user, code='fresh')
        self.assertEqual(str(caught.exception.detail['code']), 'PRICE_CHANGED')
        self.assertEqual(adapter.calls, [])
        self.assertFalse(ClientWalletLedger.objects.filter(entry_type='shared_spend').exists())

    def test_dispatching_reentry_does_not_authenticate_or_dispatch_twice(self):
        adapter = RecordingAdapter(self)
        real_spend = adapter.spend
        nested = []
        def capture(attempt, session):
            with patch.object(adapter, 'authenticate', side_effect=AssertionError('no second login')):
                nested.append(pay(self.order.order_no, self.user, code='other-device'))
            return real_spend(attempt, session)
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter), \
             patch.object(adapter, 'spend', side_effect=capture):
            result = pay(self.order.order_no, self.user, code='first-device')
        self.assertEqual(result['status'], 'paid')
        self.assertEqual(nested[0]['status'], 'unknown')
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(WalletSpendAttempt.objects.count(), 1)
        self.assertEqual(ClientWalletLedger.objects.filter(entry_type='shared_spend').count(), 1)

    def test_late_request_sees_racing_reservation_and_never_authenticates(self):
        from .order_spend import prepare
        from . import spend_service as spend
        actual_reserved = spend.reserved
        created = []
        def racing_reservation(wallet, exclude=None):
            # Reproduce a commit between the first binding read and admission.
            if not created:
                created.append(None)
                created[0] = prepare(self.order.order_no, self.user)
            return actual_reserved(wallet, exclude=exclude)
        with patch('apps.wallet.order_spend.spend.reserved', side_effect=racing_reservation), \
             patch('apps.wallet.spend_adapter.WeChatSpendAdapter') as factory:
            with self.assertRaises(ValidationError) as caught:
                pay(self.order.order_no, self.user, code='late-device')
        self.assertEqual(str(caught.exception.detail['code']), 'PAYMENT_PENDING')
        factory.assert_not_called()
        self.assertEqual(WalletSpendAttempt.objects.count(), 1)
        self.assertEqual(created[0].status, 'prepared')
        self.assertFalse(ClientWalletLedger.objects.filter(entry_type='shared_spend').exists())
