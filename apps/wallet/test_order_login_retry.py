"""Real order/payment persistence with isolated fake WeChat boundaries."""
from unittest.mock import patch
from django.test import TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError
from apps.payments.virtualpay import VirtualPaymentAPIError
from . import test_order_durable as fixtures
from .test_order_durable import RecordingAdapter
from .models import WalletSpendAttempt, ClientWalletLedger
from .spend_models import OrderWalletSpend
from .coin_balance_service import pay_order_with_coin_aware_balance as pay


@override_settings(WECHAT_VIRTUALPAY_ENV=1, WECHAT_VIRTUALPAY_ENABLED=True,
                   SHARED_SPEND_PLATFORM_APPROVED=True, WECHAT_VIRTUALPAY_COIN_UNITS_PER_YUAN=10)
class OrderLoginRetryTests(TransactionTestCase):
    def setUp(self):
        fixtures.OriginalOrderDurableTests.setUp(self)

    def test_login_failure_does_not_bind_order_then_same_order_can_pay(self):
        adapter = RecordingAdapter(self)
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter):
            with patch.object(adapter, 'authenticate', side_effect=VirtualPaymentAPIError(40163, 'code been used')):
                with self.assertRaises((VirtualPaymentAPIError, ValidationError)):
                    pay(self.order.order_no, self.user, code='expired-test-code')
            self.assertFalse(OrderWalletSpend.objects.filter(order=self.order).exists())
            self.assertFalse(WalletSpendAttempt.objects.exists())
            self.assertEqual(adapter.calls, [])
            self.wallet.refresh_from_db()
            self.assertEqual(str(self.wallet.balance), '10.00')
            result = pay(self.order.order_no, self.user, code='fresh-test-code')
        self.assertEqual(result['status'], 'paid')
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(ClientWalletLedger.objects.filter(entry_type='shared_spend').count(), 1)

    def test_login_error_http_is_explicit_and_logs_are_redacted(self):
        from rest_framework.test import APIClient
        client = APIClient(); client.force_authenticate(self.user)
        adapter = RecordingAdapter(self)
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter), \
             patch.object(adapter, 'authenticate', side_effect=VirtualPaymentAPIError(40163, 'secret-test-code sensitive-session')), \
             self.assertLogs('apps.wallet.order_login', level='WARNING') as logs:
            response = client.post('/api/pay/balance/create', {
                'order_no': self.order.order_no, 'code': 'secret-test-code'}, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(str(response.data['code']), 'WECHAT_LOGIN_RETRYABLE')
        self.assertEqual(str(response.data['wechat_code']), '40163')
        output = str(response.data) + ' '.join(logs.output)
        self.assertNotIn('secret-test-code', output)
        self.assertNotIn('sensitive-session', output)
        self.assertIn(self.order.order_no, ' '.join(logs.output))
        self.assertFalse(WalletSpendAttempt.objects.exists())

    def test_real_adapter_session_is_not_persisted_and_is_exchanged_once(self):
        session = 'ephemeral-secret-session'
        with patch('apps.wallet.coin_sync.exchange_code_for_session', return_value=(self.profile.openid, session)) as login, \
             patch('apps.wallet.spend_adapter.user_xpay_post', side_effect=lambda endpoint, payload, key, **kw: {'errcode': 0, 'order_id': payload['order_id']}) as debit:
            result = pay(self.order.order_no, self.user, code='ephemeral-secret-code')
        self.assertEqual(result['status'], 'paid')
        login.assert_called_once_with('ephemeral-secret-code')
        self.assertEqual(debit.call_args.args[2], session)
        a = WalletSpendAttempt.objects.get()
        stored = str(list(WalletSpendAttempt.objects.values())) + str(list(a.audit_events.values()))
        self.assertNotIn(session, stored)
        self.assertNotIn('ephemeral-secret-code', stored)

    def test_login_failure_on_existing_prepared_intent_is_retryable(self):
        from .order_spend import prepare
        a = prepare(self.order.order_no, self.user)
        adapter = RecordingAdapter(self)
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter):
            with patch.object(adapter, 'authenticate', side_effect=VirtualPaymentAPIError(40163, 'used')):
                with self.assertRaises(ValidationError):
                    pay(self.order.order_no, self.user, code='bad')
            a.refresh_from_db()
            self.assertEqual(a.status, 'prepared')
            self.assertEqual(str(a.reserved_amount), '6.00')
            result = pay(self.order.order_no, self.user, code='good')
        self.assertEqual(result['status'], 'paid')
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(WalletSpendAttempt.objects.count(), 1)

    def test_unknown_retry_does_not_authenticate_or_spend_again(self):
        adapter = RecordingAdapter(self, timeout=True)
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter):
            self.assertEqual(pay(self.order.order_no, self.user, code='first')['status'], 'unknown')
            with patch.object(adapter, 'authenticate', side_effect=AssertionError('must not reauthenticate')):
                self.assertEqual(pay(self.order.order_no, self.user, code='next')['status'], 'unknown')
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(WalletSpendAttempt.objects.get().status, 'unknown')

    def test_historical_failed_attempt_is_not_reopened(self):
        from .order_spend import prepare
        from .spend_service import cancel_prepared
        a = prepare(self.order.order_no, self.user)
        cancel_prepared(a.pk, reason='AUTHENTICATION_FAILED')
        before = list(WalletSpendAttempt.objects.values())
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter') as factory:
            self.assertEqual(pay(self.order.order_no, self.user, code='fresh')['status'], 'failed')
        factory.assert_not_called()
        self.assertEqual(before, list(WalletSpendAttempt.objects.values()))

    def test_login_network_failure_does_not_bind_or_enable_auto_retry(self):
        adapter = RecordingAdapter(self)
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter), \
             patch.object(adapter, 'authenticate', side_effect=VirtualPaymentAPIError('jscode2session_failed', 'secret URL')):
            with self.assertRaises(ValidationError) as error:
                pay(self.order.order_no, self.user, code='fresh')
        self.assertEqual(str(error.exception.detail['code']), 'WECHAT_LOGIN_FAILED')
        self.assertFalse(WalletSpendAttempt.objects.exists())
        self.assertNotIn('secret URL', str(error.exception.detail))

    def test_identity_mismatch_never_becomes_auto_retry(self):
        with patch('apps.wallet.coin_sync.exchange_code_for_session', return_value=('wrong-account', 'secret')), \
             self.assertLogs('apps.wallet.order_login', level='WARNING') as logs:
            with self.assertRaises(ValidationError) as error:
                pay(self.order.order_no, self.user, code='fresh')
        self.assertIn('reason=WECHAT_IDENTITY_MISMATCH', ' '.join(logs.output))
        self.assertNotEqual(str(error.exception.detail.get('code')), 'WECHAT_LOGIN_RETRYABLE')
        self.assertFalse(WalletSpendAttempt.objects.exists())

    def test_concurrent_requests_have_one_capture(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import connections
        from django.contrib.auth.models import User
        barrier = Barrier(2)
        adapter = RecordingAdapter(self)
        def authenticate(attempt, code):
            barrier.wait(timeout=10)
            return 'same-user-request-scoped-session'
        def worker(code):
            try:
                user = User.objects.get(pk=self.user.pk)
                return pay(self.order.order_no, user, code=code)['status']
            finally:
                connections.close_all()
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter), \
             patch.object(adapter, 'authenticate', side_effect=authenticate), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, c) for c in ('device-a', 'device-b')]
            results = [f.result(timeout=20) for f in futures]
        self.assertTrue(all(r in ('paid', 'unknown', 'processing') for r in results), results)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(WalletSpendAttempt.objects.count(), 1)
        self.assertEqual(ClientWalletLedger.objects.filter(entry_type='shared_spend').count(), 1)

    def _pending_refund_and_new_order(self):
        from apps.orders.models import Order
        from rest_framework.test import APIClient
        from .coin_sync import has_pending_coin_refunds
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=RecordingAdapter(self)):
            pay(self.order.order_no, self.user, code='initial')
        Order.objects.filter(pk=self.order.pk).update(status=Order.STATUS_WAITING)
        client = APIClient(); client.force_authenticate(self.user)
        response = client.post('/api/boss/order/' + self.order.order_no + '/cancel', {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(has_pending_coin_refunds(self.profile))
        return Order.objects.create(order_no='REFUND-GATE-ORDER', boss_user=self.user,
            package=self.package, required_players=1, total_price_per_hour=6, total_amount=6,
            status=Order.STATUS_PENDING_PAYMENT)

    def test_price_changed_during_refund_login_syncs_refund_without_new_debit(self):
        from apps.orders.models import Order
        from .coin_sync import has_pending_coin_refunds
        new = self._pending_refund_and_new_order()
        ledger_count = ClientWalletLedger.objects.filter(entry_type='shared_spend').count()

        def exchange(code):
            Order.objects.filter(pk=new.pk).update(total_amount=7)
            return self.profile.openid, 'refund-session'

        with patch('apps.wallet.coin_sync.exchange_code_for_session', side_effect=exchange) as login, \
             patch('apps.wallet.coin_sync.user_xpay_post', return_value={'errcode': 0}) as refund_remote, \
             patch('apps.wallet.spend_adapter.user_xpay_post', side_effect=lambda endpoint, payload, key, **kw: {'errcode': 0, 'order_id': payload['order_id']}) as debit:
            with self.assertRaises(ValidationError) as error:
                pay(new.order_no, self.user, code='refund-code')
        self.assertEqual(str(error.exception.detail['code']), 'PRICE_CHANGED')
        login.assert_called_once_with('refund-code')
        refund_remote.assert_called_once()
        self.assertFalse(has_pending_coin_refunds(self.profile))
        debit.assert_not_called()
        self.assertFalse(OrderWalletSpend.objects.filter(order=new).exists())
        self.assertEqual(WalletSpendAttempt.objects.count(), 1)
        self.assertEqual(ClientWalletLedger.objects.filter(entry_type='shared_spend').count(), ledger_count)

    def test_disabled_new_pay_still_syncs_committed_refund_without_debit(self):
        from .coin_sync import has_pending_coin_refunds
        new = self._pending_refund_and_new_order()
        with override_settings(WECHAT_VIRTUALPAY_ENABLED=False), \
             patch('apps.wallet.coin_sync.exchange_code_for_session', return_value=(self.profile.openid, 'refund-session')) as login, \
             patch('apps.wallet.coin_sync.user_xpay_post', return_value={'errcode': 0}) as refund_remote, \
             patch('apps.wallet.spend_adapter.user_xpay_post') as debit:
            with self.assertRaises(ValidationError):
                pay(new.order_no, self.user, code='refund-code')
        refund_remote.assert_called_once()
        login.assert_called_once_with('refund-code')
        debit.assert_not_called()
        self.assertFalse(has_pending_coin_refunds(self.profile))
        self.assertFalse(OrderWalletSpend.objects.filter(order=new).exists())
        self.assertEqual(WalletSpendAttempt.objects.count(), 1)

    def test_expired_order_still_syncs_committed_refund(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.orders.payment_deadlines import persist_payment_window
        new = self._pending_refund_and_new_order()
        persist_payment_window(new, started_at=timezone.now() - timedelta(hours=1), force_reset=True)
        self._assert_refund_synced_without_new_debit(new)

    def test_insufficient_balance_still_syncs_committed_refund(self):
        from apps.orders.models import Order
        new = self._pending_refund_and_new_order()
        Order.objects.filter(pk=new.pk).update(total_amount=100)
        self._assert_refund_synced_without_new_debit(new)

    def _assert_refund_synced_without_new_debit(self, new):
        from .coin_sync import has_pending_coin_refunds
        with patch('apps.wallet.coin_sync.exchange_code_for_session', return_value=(self.profile.openid, 'refund-session')) as login, \
             patch('apps.wallet.coin_sync.user_xpay_post', return_value={'errcode': 0}) as refund_remote, \
             patch('apps.wallet.spend_adapter.user_xpay_post') as debit:
            with self.assertRaises(ValidationError):
                pay(new.order_no, self.user, code='refund-code')
        refund_remote.assert_called_once()
        login.assert_called_once_with('refund-code')
        debit.assert_not_called()
        self.assertFalse(has_pending_coin_refunds(self.profile))
        self.assertFalse(OrderWalletSpend.objects.filter(order=new).exists())
        self.assertEqual(WalletSpendAttempt.objects.count(), 1)

    def test_unapproved_refund_does_not_sync_or_exchange_code(self):
        from .coin_sync import has_pending_coin_refunds
        new = self._pending_refund_and_new_order()
        with override_settings(SHARED_SPEND_PLATFORM_APPROVED=False), \
             patch('apps.wallet.coin_sync.exchange_code_for_session') as login, \
             patch('apps.wallet.coin_sync.user_xpay_post') as refund_remote, \
             patch('apps.wallet.spend_adapter.user_xpay_post') as debit:
            with self.assertRaises(ValidationError):
                pay(new.order_no, self.user, code='refund-code')
        login.assert_not_called()
        refund_remote.assert_not_called()
        debit.assert_not_called()
        self.assertTrue(has_pending_coin_refunds(self.profile))
        self.assertFalse(OrderWalletSpend.objects.filter(order=new).exists())

    def test_wrong_identity_does_not_sync_refund(self):
        from .coin_sync import has_pending_coin_refunds
        new = self._pending_refund_and_new_order()
        with patch('apps.wallet.coin_sync.exchange_code_for_session', return_value=('wrong-account', 'secret')), \
             patch('apps.wallet.coin_sync.user_xpay_post') as refund_remote, \
             patch('apps.wallet.spend_adapter.user_xpay_post') as debit:
            with self.assertRaises(ValidationError):
                pay(new.order_no, self.user, code='refund-code')
        refund_remote.assert_not_called()
        debit.assert_not_called()
        self.assertTrue(has_pending_coin_refunds(self.profile))
        self.assertFalse(OrderWalletSpend.objects.filter(order=new).exists())

    def test_refund_sync_and_new_debit_share_one_login_exchange(self):
        from apps.orders.models import Order
        from rest_framework.test import APIClient
        from .coin_sync import has_pending_coin_refunds
        from apps.payments.models import Refund
        adapter = RecordingAdapter(self)
        with patch('apps.wallet.spend_adapter.WeChatSpendAdapter', return_value=adapter):
            pay(self.order.order_no, self.user, code='initial')
        Order.objects.filter(pk=self.order.pk).update(status=Order.STATUS_WAITING)
        client = APIClient(); client.force_authenticate(self.user)
        response = client.post('/api/boss/order/' + self.order.order_no + '/cancel', {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(has_pending_coin_refunds(self.profile))
        new = Order.objects.create(order_no='AFTER-REFUND', boss_user=self.user,
            package=self.package, required_players=1, total_price_per_hour=6, total_amount=6,
            status=Order.STATUS_PENDING_PAYMENT)
        with patch('apps.wallet.coin_sync.exchange_code_for_session', side_effect=[(self.profile.openid, 'single-session'), VirtualPaymentAPIError(40163, 'reused')]) as login, \
             patch('apps.wallet.coin_sync.user_xpay_post', return_value={'errcode': 0}) as refund_remote, \
             patch('apps.wallet.spend_adapter.user_xpay_post', side_effect=lambda endpoint, payload, key, **kw: {'errcode': 0, 'order_id': payload['order_id']}) as debit_remote:
            self.assertEqual(pay(new.order_no, self.user, code='single-use-code')['status'], 'paid')
        login.assert_called_once_with('single-use-code')
        self.assertEqual(refund_remote.call_count, 1)
        self.assertEqual(debit_remote.call_count, 1)
        self.assertEqual(refund_remote.call_args.args[2], 'single-session')
        self.assertEqual(debit_remote.call_args.args[2], 'single-session')
        self.assertFalse(has_pending_coin_refunds(self.profile))
        self.assertEqual(Refund.objects.count(), 1)
