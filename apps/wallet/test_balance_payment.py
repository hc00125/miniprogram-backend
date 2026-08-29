from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order, OrderStatusLog
from apps.payments.models import Payment, Refund

from .models import ClientWallet, ClientWalletLedger
from .services import pay_order_with_balance


VIRTUAL_SETTINGS = {
    'WECHAT_VIRTUALPAY_ENABLED': True,
    'WECHAT_APP_ID': 'wx_test_appid',
    'WECHAT_APP_SECRET': 'test_secret',
    'WECHAT_VIRTUALPAY_OFFER_ID': 'test_offer',
    'WECHAT_VIRTUALPAY_APP_KEY': 'test_app_key',
    'WECHAT_VIRTUALPAY_ENV': 0,
    'WECHAT_VIRTUALPAY_HTTP_TIMEOUT': 10,
}


class BalancePaymentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='balance-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='openid_balance_boss',
            nickname='余额支付测试老板',
        )
        self.package = Package.objects.create(name='余额支付测试套餐', player_count=1, base_price=30)
        self.order = Order.objects.create(
            order_no='BALANCE_ORDER_001',
            boss_user=self.user,
            boss_wechat='balance_boss',
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

    def test_successful_balance_payment(self):
        result = pay_order_with_balance(self.order.order_no, self.user)

        self.assertEqual(result['order_no'], self.order.order_no)
        self.assertEqual(result['status'], 'paid')
        self.assertEqual(result['amount'], '30.00')
        self.assertEqual(result['balance'], '70.00')

        self.order.refresh_from_db()
        self.assertTrue(self.order.paid)
        self.assertEqual(self.order.status, Order.STATUS_READY_TO_START)
        self.assertEqual(self.order.payment_method, 'balance')
        self.assertIsNotNone(self.order.payment_confirmed_at)
        self.assertTrue(
            OrderStatusLog.objects.filter(
                order=self.order,
                from_status=Order.STATUS_PENDING_PAYMENT,
                to_status=Order.STATUS_READY_TO_START,
            ).exists()
        )

        payment = Payment.objects.get(payment_no=result['payment_no'])
        self.assertEqual(payment.channel, 'balance')
        self.assertEqual(payment.scene, 'balance')
        self.assertEqual(payment.status, 'paid')
        self.assertEqual(payment.amount, 30.0)
        self.assertIsNotNone(payment.paid_at)

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('70.00'))
        self.assertEqual(self.wallet.spent_total, Decimal('30.00'))

        entry = ClientWalletLedger.objects.get()
        self.assertEqual(entry.entry_type, ClientWalletLedger.TYPE_ORDER_PAYMENT)
        self.assertEqual(entry.amount, Decimal('-30.00'))
        self.assertEqual(entry.balance_after, Decimal('70.00'))
        self.assertEqual(entry.reference_id, payment.payment_no)

    def test_insufficient_balance_rejected(self):
        self.wallet.balance = Decimal('10.00')
        self.wallet.save(update_fields=['balance', 'updated_at'])

        with self.assertRaises(ValidationError) as ctx:
            pay_order_with_balance(self.order.order_no, self.user)
        self.assertEqual(ctx.exception.detail['detail'], '余额不足')

        self.order.refresh_from_db()
        self.assertFalse(self.order.paid)
        self.assertEqual(self.order.status, Order.STATUS_PENDING_PAYMENT)
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(ClientWalletLedger.objects.exists())
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('10.00'))

    def test_repeat_call_does_not_double_charge(self):
        pay_order_with_balance(self.order.order_no, self.user)
        with self.assertRaises(ValidationError) as ctx:
            pay_order_with_balance(self.order.order_no, self.user)
        self.assertEqual(ctx.exception.detail['detail'], '订单已支付')

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('70.00'))
        self.assertEqual(self.wallet.spent_total, Decimal('30.00'))
        self.assertEqual(ClientWalletLedger.objects.count(), 1)
        self.assertEqual(Payment.objects.filter(channel='balance').count(), 1)

    def test_active_wechat_virtual_payment_blocks_balance_payment(self):
        Payment.objects.create(
            payment_no='VIRTUAL_ACTIVE_001',
            order=self.order,
            channel='wechat_virtual',
            scene='short_series_goods',
            amount=30,
            status='paying',
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        with self.assertRaises(ValidationError) as ctx:
            pay_order_with_balance(self.order.order_no, self.user)
        self.assertEqual(
            ctx.exception.detail['detail'],
            '存在进行中的微信支付，请完成或等待其过期后再用余额支付',
        )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('100.00'))
        self.assertFalse(ClientWalletLedger.objects.exists())

    @override_settings(**VIRTUAL_SETTINGS)
    def test_expired_wechat_virtual_payment_closed_after_remote_confirms_unpaid(self):
        stale = Payment.objects.create(
            payment_no='VIRTUAL_EXPIRED_001',
            order=self.order,
            channel='wechat_virtual',
            scene='short_series_goods',
            amount=30,
            status='paying',
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        with patch(
            'apps.wallet.services.query_virtual_payment',
            return_value=stale,
        ) as mocked_query:
            result = pay_order_with_balance(self.order.order_no, self.user)

        mocked_query.assert_called_once_with(stale.payment_no, self.user)
        self.assertEqual(result['status'], 'paid')
        stale.refresh_from_db()
        self.assertEqual(stale.status, 'closed')
        self.order.refresh_from_db()
        self.assertTrue(self.order.paid)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('70.00'))

    @override_settings(**VIRTUAL_SETTINGS)
    def test_late_wechat_capture_after_balance_payment_not_double_marked(self):
        """订单已余额支付后，被关闭的虚拟支付单收到远端“已支付”结果：
        不得重新标记为 paid、不得覆盖支付方式、不得 ack 发货（留给微信自动退款）。"""
        stale = Payment.objects.create(
            payment_no='VIRTUAL_LATE_001',
            order=self.order,
            channel='wechat_virtual',
            scene='short_series_goods',
            amount=30,
            status='paying',
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        with patch('apps.wallet.services.query_virtual_payment', return_value=stale):
            pay_order_with_balance(self.order.order_no, self.user)
        stale.refresh_from_db()
        self.assertEqual(stale.status, 'closed')

        # 用户手机上仍存活的支付面板此刻完成了支付：前端轮询查单命中已关闭的旧单。
        from apps.payments.virtualpay import query_virtual_payment as real_query

        paid_response = {
            'errcode': 0,
            'errmsg': 'OK',
            'order': {'status': 2, 'order_fee': 3000, 'paid_fee': 3000, 'wx_order_id': 'WX_LATE_001'},
        }
        with patch('apps.payments.virtualpay.xpay_post', return_value=paid_response) as mocked_xpay:
            real_query(stale.payment_no, self.user)

        stale.refresh_from_db()
        self.assertEqual(stale.status, 'closed')
        self.assertIn('late_capture', stale.notify_payload)
        self.order.refresh_from_db()
        self.assertTrue(self.order.paid)
        self.assertEqual(self.order.payment_method, 'balance')
        # 仅有查单调用，绝无 notify_provide_goods 发货 ack。
        endpoints = [call.args[0] for call in mocked_xpay.call_args_list]
        self.assertEqual(endpoints, ['/xpay/query_order'])
        # 钱包扣款保持不变，未发生第二次账务变化。
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('70.00'))
        self.assertEqual(ClientWalletLedger.objects.count(), 1)

    def test_jsapi_paying_payment_blocks_balance_payment(self):
        """非虚拟通道（jsapi）的进行中支付无法远程核实，即使已过期也拒绝余额支付。"""
        Payment.objects.create(
            payment_no='JSAPI_PAYING_001',
            order=self.order,
            channel='wechat',
            scene='jsapi',
            amount=30,
            status='paying',
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        with self.assertRaises(ValidationError) as ctx:
            pay_order_with_balance(self.order.order_no, self.user)
        self.assertEqual(
            ctx.exception.detail['detail'],
            '存在进行中的微信支付，请完成或等待其过期后再用余额支付',
        )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('100.00'))
        self.assertFalse(ClientWalletLedger.objects.exists())

    @override_settings(**VIRTUAL_SETTINGS)
    def test_concurrent_virtual_payment_created_during_precheck_rejected(self):
        """预检远程查单期间并发新建的未过期虚拟支付单必须被事务内重查拦下（TOCTOU）。"""
        stale = Payment.objects.create(
            payment_no='VIRTUAL_EXPIRED_RACE',
            order=self.order,
            channel='wechat_virtual',
            scene='short_series_goods',
            amount=30,
            status='paying',
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        def fake_query(payment_no, user):
            Payment.objects.create(
                payment_no='VIRTUAL_RACE_NEW',
                order=self.order,
                channel='wechat_virtual',
                scene='short_series_goods',
                amount=30,
                status='paying',
                expires_at=timezone.now() + timedelta(minutes=9),
            )
            return stale

        with patch('apps.wallet.services.query_virtual_payment', side_effect=fake_query):
            with self.assertRaises(ValidationError) as ctx:
                pay_order_with_balance(self.order.order_no, self.user)
        self.assertEqual(
            ctx.exception.detail['detail'],
            '存在进行中的微信支付，请完成或等待其过期后再用余额支付',
        )
        stale.refresh_from_db()
        self.assertEqual(stale.status, 'paying')
        self.order.refresh_from_db()
        self.assertFalse(self.order.paid)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('100.00'))
        self.assertFalse(ClientWalletLedger.objects.exists())

    def test_balance_payment_via_api_route(self):
        client_api = APIClient()
        client_api.force_authenticate(self.user)
        response = client_api.post(
            '/api/pay/balance/create',
            {'order_no': self.order.order_no},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'paid')
        self.assertEqual(response.data['balance'], '70.00')
        self.order.refresh_from_db()
        self.assertTrue(self.order.paid)

    def test_insufficient_balance_via_api_returns_detail(self):
        self.wallet.balance = Decimal('1.00')
        self.wallet.save(update_fields=['balance', 'updated_at'])
        client_api = APIClient()
        client_api.force_authenticate(self.user)
        response = client_api.post(
            '/api/pay/balance/create',
            {'order_no': self.order.order_no},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['detail'], '余额不足')


class BalanceRefundTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='refund-boss')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            openid='openid_refund_boss',
            nickname='余额退款测试老板',
        )
        self.package = Package.objects.create(name='余额退款测试套餐', player_count=1, base_price=30)
        self.order = Order.objects.create(
            order_no='BALANCE_ORDER_REF_001',
            boss_user=self.user,
            boss_wechat='refund_boss',
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

    def test_refund_succeeded_returns_balance_exactly_once(self):
        refund = Refund.objects.create(
            refund_no='REFBAL0001',
            payment=self.payment,
            order=self.order,
            amount=30.0,
            status=Refund.STATUS_PENDING,
        )
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('70.00'))

        refund.status = Refund.STATUS_SUCCEEDED
        refund.save(update_fields=['status', 'updated_at'])

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('100.00'))
        refund_entries = ClientWalletLedger.objects.filter(
            entry_type=ClientWalletLedger.TYPE_REFUND_IN,
        )
        self.assertEqual(refund_entries.count(), 1)
        entry = refund_entries.get()
        self.assertEqual(entry.amount, Decimal('30.00'))
        self.assertEqual(entry.balance_after, Decimal('100.00'))
        self.assertEqual(entry.reference_id, refund.refund_no)

        # 信号重放（重复 save）不重复入账。
        refund.save()
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('100.00'))
        self.assertEqual(refund_entries.count(), 1)

    def test_wechat_channel_refund_does_not_touch_wallet(self):
        wechat_payment = Payment.objects.create(
            payment_no='WECHAT_PAY_REF_001',
            order=self.order,
            channel='wechat_virtual',
            scene='short_series_goods',
            amount=30,
            status='paid',
        )
        refund = Refund.objects.create(
            refund_no='REFWX0001',
            payment=wechat_payment,
            order=self.order,
            amount=30.0,
            status=Refund.STATUS_SUCCEEDED,
        )
        self.assertIsNotNone(refund)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('70.00'))
        self.assertFalse(
            ClientWalletLedger.objects.filter(
                entry_type=ClientWalletLedger.TYPE_REFUND_IN,
            ).exists()
        )
