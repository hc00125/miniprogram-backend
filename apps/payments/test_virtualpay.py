from unittest.mock import call, patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from apps.accounts.models import ClientProfile
from apps.catalog.models import Package
from apps.orders.models import Order, OrderItem

from .models import Payment
from .virtualpay import (
    VirtualPaymentAPIError,
    hmac_sha256_hex,
    query_virtual_payment,
    resolve_virtual_product,
    xpay_post,
)


@override_settings(
    WECHAT_VIRTUALPAY_ENV=1,
    WECHAT_VIRTUALPAY_SANDBOX_PRODUCT_ID='escort_15',
    WECHAT_VIRTUALPAY_SANDBOX_PRICE_FEN=1500,
    WECHAT_VIRTUALPAY_SANDBOX_PACKAGE_KEYWORD='四套四弹',
)
class VirtualPaymentSandboxTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='virtual-pay-user')
        ClientProfile.objects.create(user=self.user, openid='openid-test', nickname='测试老板')
        self.package = Package.objects.create(
            name='四套四弹娱乐陪',
            base_price=15,
            player_count=4,
            is_active=True,
        )
        self.order = Order.objects.create(
            order_no='20260710123456ABCD',
            boss_user=self.user,
            boss_wechat='test-wechat',
            package=self.package,
            required_players=4,
            total_price_per_hour=15,
            total_amount=15,
            status=Order.STATUS_PENDING_PAYMENT,
        )
        OrderItem.objects.create(
            order=self.order,
            package=self.package,
            package_name=self.package.name,
            unit_price=15,
            quantity=1,
            amount=15,
        )

    def test_sandbox_fallback_resolves_escort_15(self):
        result = resolve_virtual_product(self.order)
        self.assertEqual(result['product_id'], 'escort_15')
        self.assertEqual(result['goods_price_fen'], 1500)
        self.assertEqual(result['quantity'], 1)
        self.assertEqual(result['expected_total_fen'], 1500)

    def test_hmac_signature_is_stable(self):
        result = hmac_sha256_hex('key', 'requestVirtualPayment&{}')
        self.assertEqual(len(result), 64)
        self.assertEqual(result, hmac_sha256_hex('key', 'requestVirtualPayment&{}'))


VIRTUAL_SETTINGS = {
    'WECHAT_VIRTUALPAY_ENABLED': True,
    'WECHAT_APP_ID': 'wx_test_appid',
    'WECHAT_APP_SECRET': 'test_secret',
    'WECHAT_VIRTUALPAY_OFFER_ID': 'test_offer',
    'WECHAT_VIRTUALPAY_APP_KEY': 'test_app_key',
    'WECHAT_VIRTUALPAY_ENV': 0,
    'WECHAT_VIRTUALPAY_HTTP_TIMEOUT': 10,
}


@override_settings(**VIRTUAL_SETTINGS)
class VirtualPaymentRecoveryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='virtual-boss', password='pass')
        ClientProfile.objects.create(
            user=self.user,
            openid='openid_virtual_boss',
            nickname='虚拟支付测试老板',
        )
        self.package = Package.objects.create(name='虚拟支付测试套餐', player_count=1, base_price=35)
        self.order = Order.objects.create(
            order_no='VIRTUAL_ORDER_001',
            boss_user=self.user,
            boss_wechat='virtual_boss',
            package=self.package,
            required_players=1,
            total_price_per_hour=35,
            total_amount=35,
            status=Order.STATUS_PENDING_PAYMENT,
        )
        self.payment = Payment.objects.create(
            payment_no='VIRTUAL_PAY_001',
            order=self.order,
            channel='wechat_virtual',
            scene='short_series_goods',
            amount=35,
            status='paying',
            third_order_no='VIRTUAL_PAY_001',
            notify_payload={
                'virtual_payment': True,
                'env': 0,
                'delivery_status': 'pending',
                'product_id': 'nvpei_44',
                'goods_price_fen': 3500,
                'quantity': 1,
                'expected_total_fen': 3500,
            },
        )

    @patch('apps.payments.virtualpay.xpay_post')
    def test_delivery_failure_does_not_rollback_confirmed_payment(self, mocked_xpay_post):
        mocked_xpay_post.side_effect = [
            {
                'errcode': 0,
                'errmsg': 'OK',
                'order': {
                    'status': 3,
                    'order_fee': 3500,
                    'paid_fee': 3500,
                    'wx_order_id': 'WX_VIRTUAL_001',
                },
            },
            VirtualPaymentAPIError(40001, 'invalid credential'),
        ]

        result = query_virtual_payment(self.payment.payment_no, self.user)

        self.payment.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(result.status, 'paid')
        self.assertEqual(self.payment.status, 'paid')
        self.assertTrue(self.order.paid)
        self.assertEqual(self.order.status, Order.STATUS_READY_TO_START)
        self.assertEqual(self.payment.notify_payload['delivery_status'], 'failed')
        self.assertEqual(self.payment.notify_payload['delivery_error']['code'], 40001)
        self.assertEqual(
            mocked_xpay_post.call_args_list,
            [
                call('/xpay/query_order', {
                    'openid': 'openid_virtual_boss',
                    'env': 0,
                    'order_id': 'VIRTUAL_PAY_001',
                }),
                call('/xpay/notify_provide_goods', {
                    'order_id': 'VIRTUAL_PAY_001',
                    'env': 0,
                }),
            ],
        )

    @patch('apps.payments.virtualpay.xpay_post')
    def test_paid_payment_retries_failed_delivery(self, mocked_xpay_post):
        self.payment.status = 'paid'
        self.payment.notify_payload = {
            **self.payment.notify_payload,
            'delivery_status': 'failed',
            'delivery_error': {'code': 40001, 'message': 'invalid credential'},
        }
        self.payment.save(update_fields=['status', 'notify_payload'])
        self.order.paid = True
        self.order.status = Order.STATUS_READY_TO_START
        self.order.save(update_fields=['paid', 'status'])
        mocked_xpay_post.return_value = {'errcode': 0, 'errmsg': 'OK'}

        query_virtual_payment(self.payment.payment_no, self.user)

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.notify_payload['delivery_status'], 'succeeded')
        self.assertEqual(self.payment.notify_payload['delivery_response']['errcode'], 0)
        self.assertNotIn('delivery_error', self.payment.notify_payload)
        mocked_xpay_post.assert_called_once_with('/xpay/notify_provide_goods', {
            'order_id': 'VIRTUAL_PAY_001',
            'env': 0,
        })


@override_settings(**VIRTUAL_SETTINGS)
class StableAccessTokenRetryTests(TestCase):
    @patch('apps.payments.virtualpay.cache.delete')
    @patch('apps.payments.virtualpay.get_access_token')
    @patch('apps.payments.virtualpay._xpay_post_once')
    def test_xpay_retries_once_after_invalid_token(
        self,
        mocked_post_once,
        mocked_get_access_token,
        mocked_cache_delete,
    ):
        mocked_get_access_token.side_effect = ['old-token', 'new-token']
        mocked_post_once.side_effect = [
            {'errcode': 40001, 'errmsg': 'invalid credential'},
            {'errcode': 0, 'errmsg': 'OK'},
        ]

        result = xpay_post('/xpay/query_order', {'order_id': 'VIRTUAL_PAY_001'})

        self.assertEqual(result['errcode'], 0)
        self.assertEqual(mocked_post_once.call_count, 2)
        mocked_get_access_token.assert_has_calls([call(), call(force_refresh=True)])
        mocked_cache_delete.assert_called_once()
