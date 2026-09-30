"""Synthetic combined checkout through the existing WeChat coin routes."""
import json
from decimal import Decimal
from unittest.mock import patch
from django.test import TransactionTestCase, override_settings
from . import test_surcharge_v2 as fixtures


@override_settings(ORDER_SURCHARGE_ENABLED=True, WECHAT_VIRTUALPAY_ENABLED=True,
    WECHAT_APP_ID='offline-app', WECHAT_APP_SECRET='offline-secret',
    WECHAT_VIRTUALPAY_OFFER_ID='offline-offer', WECHAT_VIRTUALPAY_APP_KEY='offline-key',
    WECHAT_VIRTUALPAY_ENV=1, WECHAT_VIRTUALPAY_COIN_UNITS_PER_YUAN=10,
    ENABLE_MOCK_PAYMENT=False)
class CombinedCoinCheckoutTests(TransactionTestCase):
    def setUp(self):
        fixtures.CheckoutPaymentTests.setUp(self)

    def create_coin(self):
        with patch('apps.wallet.coin_recharge.exchange_code_for_session',
                   return_value=(self.profile.openid, 'offline-session')):
            return self.client.post('/api/pay/wechat/virtual/create',
                {'order_no': self.order.order_no, 'code': 'offline-code'},
                format='json', HTTP_X_CLIENT_PLATFORM='android')

    def test_wechat_recharges_exact_combined_total_and_reuses_original_recharge(self):
        from apps.wallet.models import RechargeOrder, WalletSpendAttempt
        from apps.payments.models import Payment
        result = self.create_coin()
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data['mode'], 'short_series_coin')
        self.assertEqual(result.data['amount'], '11.20')
        self.assertEqual(json.loads(result.data['signData'])['buyQuantity'], 112)
        self.assertEqual(result.data['checkout_order_no'], self.order.order_no)
        replay = self.create_coin()
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data['payment_no'], result.data['payment_no'])
        self.assertEqual(RechargeOrder.objects.count(), 1)
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(WalletSpendAttempt.objects.exists())
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('30.00'))

    def credit_coin(self):
        result = self.create_coin()
        self.assertEqual(result.status_code, 200, result.data)
        recharge_no = result.data['payment_no']
        with patch('apps.wallet.coin_recharge.xpay_post', return_value={'errcode': 0, 'order': {
            'status': 2, 'order_fee': 1120, 'paid_fee': 1120, 'wx_order_id': 'offline-cash-trade'}}):
            credited = self.client.post('/api/pay/wechat/virtual/query/' + recharge_no, {}, format='json')
        self.assertEqual(credited.status_code, 200, credited.data)
        self.assertEqual(credited.data['status'], 'credited')
        return recharge_no

    def finalize_coin(self, recharge_no):
        with patch('apps.wallet.coin_sync.exchange_code_for_session',
                   return_value=(self.profile.openid, 'offline-fresh-session')):
            return self.client.post('/api/pay/wechat/virtual/finalize/' + recharge_no,
                {'code': 'offline-fresh-code'}, format='json')

    def test_credited_coin_pays_combined_total_once_and_cancel_refunds_both_parts(self):
        from apps.wallet.models import ClientWalletLedger, WalletSpendAttempt
        from apps.payments.models import Payment, Refund
        from apps.orders.surcharge_models import OrderCheckoutRefund
        recharge_no = self.credit_coin()
        with patch('apps.wallet.spend_adapter.user_xpay_post',
                   side_effect=lambda endpoint, payload, session, **kwargs: {'errcode': 0, 'order_id': payload['order_id']}) as debit:
            result = self.finalize_coin(recharge_no)
            self.assertEqual(result.status_code, 200, result.data)
            self.assertEqual(result.data['status'], 'paid')
            self.assertEqual(result.data['amount'], '11.20')
            replay = self.finalize_coin(recharge_no)
            self.assertEqual(replay.status_code, 200, replay.data)
            self.assertEqual(replay.data['amount'], '11.20')
            self.assertEqual(debit.call_count, 1)
            self.assertEqual(debit.call_args.args[0], '/xpay/currency_pay')
            self.assertEqual(debit.call_args.args[1]['amount'], 112)
        self.wallet.refresh_from_db(); self.order.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('30.00'))
        self.assertTrue(self.order.paid)
        self.assertEqual(Payment.objects.get(order=self.order).amount, Decimal('10.00'))
        self.assertEqual(self.order.surcharges.get().amount_yuan, Decimal('1.20'))
        attempt = WalletSpendAttempt.objects.get()
        self.assertEqual(attempt.amount, Decimal('11.20'))
        self.assertEqual(attempt.source_snapshot['allocations'][0]['reference_id'], recharge_no)
        self.assertEqual(ClientWalletLedger.objects.filter(entry_type='shared_spend').count(), 1)
        cancelled = self.client.post('/api/boss/order/' + self.order.order_no + '/cancel', {}, format='json')
        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('41.20'))
        self.assertEqual(Refund.objects.get().amount, Decimal('10.00'))
        refund = OrderCheckoutRefund.objects.get()
        self.assertEqual(refund.surcharge_amount, Decimal('1.20'))
        self.assertEqual(refund.request_payload['amount'], 112)

    def test_unconfirmed_wechat_recharge_blocks_balance_switch_and_unpaid_cancel(self):
        from apps.wallet.models import WalletSpendAttempt
        from apps.payments.models import Payment
        result = self.create_coin()
        self.assertEqual(result.status_code, 200, result.data)
        balance = self.client.post('/api/pay/balance/create', {'order_no': self.order.order_no}, format='json')
        self.assertEqual(balance.status_code, 400, balance.data)
        self.assertEqual(balance.data['code'], 'CHECKOUT_RECHARGE_PENDING')
        cancelled = self.client.post('/api/boss/order/' + self.order.order_no + '/cancel', {}, format='json')
        self.assertEqual(cancelled.status_code, 409, cancelled.data)
        self.order.refresh_from_db(); self.wallet.refresh_from_db()
        self.assertEqual(self.order.status, '待支付')
        self.assertEqual(self.wallet.balance, Decimal('30.00'))
        self.assertFalse(WalletSpendAttempt.objects.exists())
        self.assertFalse(Payment.objects.exists())

    def test_credited_recharge_for_another_order_is_never_spent_by_combined_checkout(self):
        from apps.wallet.models import RechargeOrder, ClientWalletLedger, WalletSpendAttempt
        from .models import Order
        other = Order.objects.create(order_no='other-credited', boss_user=self.user,
            package=self.order.package, required_players=3, status=Order.STATUS_PENDING_PAYMENT)
        RechargeOrder.objects.create(recharge_no='another-credited-recharge', profile=self.profile,
            amount=Decimal('10.00'), status='credited', checkout_order_no=other.order_no,
            notify_payload={'mode': 'short_series_coin', 'wechat_coin_units_per_yuan': 10})
        ClientWalletLedger.objects.create(wallet=self.wallet, entry_type='recharge', amount=Decimal('10.00'),
            balance_after=Decimal('30.00'), reference_id='another-credited-recharge')
        result = self.client.post('/api/pay/balance/create', {'order_no': self.order.order_no}, format='json')
        self.assertEqual(result.status_code, 400, result.data)
        self.assertEqual(result.data['code'], 'CHECKOUT_RECOVERY_PENDING')
        self.assertFalse(WalletSpendAttempt.objects.exists())

    def test_unknown_currency_capture_never_recharges_or_debits_again(self):
        from apps.wallet.models import WalletSpendAttempt, RechargeOrder
        recharge_no = self.credit_coin()
        with patch('apps.wallet.spend_adapter.user_xpay_post', side_effect=TimeoutError('offline lost reply')) as debit:
            first = self.finalize_coin(recharge_no)
            second = self.finalize_coin(recharge_no)
            self.assertEqual(first.data['status'], 'unknown')
            self.assertEqual(second.data['status'], 'unknown')
            self.assertEqual(debit.call_count, 1)
        created = self.create_coin()
        self.assertEqual(created.status_code, 400, created.data)
        self.assertEqual(RechargeOrder.objects.count(), 1)
        self.assertEqual(WalletSpendAttempt.objects.count(), 1)
        self.assertEqual(WalletSpendAttempt.objects.get().reserved_amount, Decimal('11.20'))

    def test_cancelled_cashier_is_confirmed_closed_then_retried_on_same_business_order(self):
        from apps.wallet.models import RechargeOrder, WalletSpendAttempt, ClientWalletLedger
        from apps.payments.models import Payment
        first = self.create_coin()
        old_no = first.data['payment_no']
        with patch('apps.wallet.coin_recharge.xpay_post', return_value={'errcode': 0,
                   'order': {'status': 6, 'order_fee': 1120}}):
            read = self.client.post('/api/pay/wechat/virtual/query-order/' + self.order.order_no, {}, format='json')
            self.assertEqual(read.status_code, 200, read.data)
            self.assertEqual(read.data['status'], 'closed')
            self.assertTrue(read.data['retry_allowed'])
            self.assertEqual(read.data['payment_no'], old_no)
            with patch('apps.wallet.coin_recharge.exchange_code_for_session',
                       return_value=(self.profile.openid, 'offline-new-session')):
                data = {'order_no': self.order.order_no, 'code': 'offline-retry-code', 'retry_from_payment_no': old_no}
                retry = self.client.post('/api/pay/wechat/virtual/create', data, format='json', HTTP_X_CLIENT_PLATFORM='android')
                replay = self.client.post('/api/pay/wechat/virtual/create', data, format='json', HTTP_X_CLIENT_PLATFORM='android')
        self.assertEqual(retry.status_code, 200, retry.data)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertNotEqual(retry.data['payment_no'], old_no)
        self.assertEqual(retry.data['payment_no'], replay.data['payment_no'])
        self.assertEqual(retry.data['retry_from_payment_no'], old_no)
        self.assertEqual(retry.data['checkout_order_no'], self.order.order_no)
        self.assertEqual(retry.data['amount'], '11.20')
        self.assertEqual(RechargeOrder.objects.get(recharge_no=old_no).status, 'closed')
        self.assertEqual(RechargeOrder.objects.count(), 2)
        self.assertFalse(WalletSpendAttempt.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(ClientWalletLedger.objects.exists())
        self.wallet.refresh_from_db(); self.order.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('30.00'))
        self.assertFalse(self.order.paid)
        self.assertEqual(self.order.status, '待支付')

    def test_closed_cancelled_cashier_past_window_retires_unpaid_order_without_money_changes(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import Order
        from .surcharge_models import OrderCheckout
        from apps.wallet.models import ClientWalletLedger, WalletSpendAttempt
        from apps.payments.models import Payment, Refund
        result = self.create_coin()
        self.assertEqual(result.status_code, 200, result.data)
        with patch('django.utils.timezone.now', return_value=timezone.now() + timedelta(minutes=20)), \
             patch('apps.wallet.coin_recharge.xpay_post', return_value={'errcode': 0,
                   'order': {'status': 6, 'order_fee': 1120}}):
            read = self.client.post('/api/pay/wechat/virtual/query-order/' + self.order.order_no, {}, format='json')
        self.assertEqual(read.status_code, 200, read.data)
        self.assertFalse(read.data['retry_allowed'])
        self.assertEqual(read.data['order_status'], '已取消')
        self.order.refresh_from_db(); self.wallet.refresh_from_db()
        self.assertEqual(self.order.status, '已取消')
        self.assertFalse(self.order.paid)
        self.assertEqual(OrderCheckout.objects.get(order=self.order).status, 'failed')
        self.assertEqual(self.wallet.balance, Decimal('30.00'))
        self.assertFalse(ClientWalletLedger.objects.exists())
        self.assertFalse(WalletSpendAttempt.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(Refund.objects.exists())

    def test_unconfirmed_or_mismatched_cashier_query_never_authorizes_retry(self):
        from apps.wallet.models import RechargeOrder, WalletSpendAttempt, ClientWalletLedger
        first = self.create_coin()
        old_no = first.data['payment_no']
        cases = [{'status': 0, 'order_fee': 1120}, {'status': 1, 'order_fee': 1120},
            {'status': 6, 'order_fee': 1100}, {'status': 6, 'order_fee': 1120, 'paid_fee': 1120},
            {'status': 6, 'order_fee': 1120, 'order_id': 'another-order'},
            {'status': 6, 'order_fee': 1120, 'paid_time': 1}]
        for data in cases:
            with self.subTest(data=data), patch('apps.wallet.coin_recharge.xpay_post',
                                               return_value={'errcode': 0, 'order': data}):
                read = self.client.post('/api/pay/wechat/virtual/query-order/' + self.order.order_no, {}, format='json')
                self.assertEqual(read.status_code, 200, read.data)
                self.assertFalse(read.data['retry_allowed'])
                retry = self.client.post('/api/pay/wechat/virtual/create', {
                    'order_no': self.order.order_no, 'code': 'unused-code', 'retry_from_payment_no': old_no},
                    format='json', HTTP_X_CLIENT_PLATFORM='android')
                self.assertEqual(retry.status_code, 400, retry.data)
                self.assertEqual(retry.data['code'], 'CHECKOUT_RECHARGE_PENDING')
        self.assertEqual(RechargeOrder.objects.count(), 1)
        self.assertEqual(RechargeOrder.objects.get().status, 'paying')
        self.assertFalse(WalletSpendAttempt.objects.exists())
        self.assertFalse(ClientWalletLedger.objects.exists())

    def test_legacy_cancelled_order_with_created_checkout_is_safely_finished_on_query(self):
        from .models import Order
        from .surcharge_models import OrderCheckout
        from apps.wallet.models import RechargeOrder, WalletSpendAttempt, ClientWalletLedger
        first = self.create_coin()
        recharge = RechargeOrder.objects.get(recharge_no=first.data['payment_no'])
        recharge.status = 'closed'
        recharge.notify_payload = {**recharge.notify_payload, 'query_order': {'status': 6, 'order_fee': 1120}}
        recharge.save(update_fields=['status', 'notify_payload'])
        Order.objects.filter(pk=self.order.pk).update(status=Order.STATUS_CANCELLED)
        self.assertEqual(OrderCheckout.objects.get(order=self.order).status, 'created')
        with patch('apps.wallet.coin_recharge.xpay_post', return_value={'errcode': 0,
                   'order': {'status': 6, 'order_fee': 1120}}):
            read = self.client.post('/api/pay/wechat/virtual/query-order/' + self.order.order_no, {}, format='json')
        self.assertEqual(read.status_code, 200, read.data)
        self.assertEqual(read.data['order_status'], '已取消')
        self.assertFalse(read.data['retry_allowed'])
        self.assertEqual(OrderCheckout.objects.get(order=self.order).status, 'failed')
        self.assertFalse(WalletSpendAttempt.objects.exists())
        self.assertFalse(ClientWalletLedger.objects.exists())

    def test_credited_checkout_can_finish_after_original_payment_window(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import Order
        recharge_no = self.credit_coin()
        Order.objects.filter(pk=self.order.pk).update(created_at=timezone.now() - timedelta(minutes=15))
        with patch('apps.wallet.spend_adapter.user_xpay_post',
                   side_effect=lambda endpoint, payload, session, **kwargs: {'errcode': 0, 'order_id': payload['order_id']}):
            result = self.finalize_coin(recharge_no)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data['status'], 'paid')
