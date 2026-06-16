import base64
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.catalog.models import Package
from apps.orders.boss_views import cancel_order as cancel_order_view
from apps.orders.boss_views import self_confirm_payment
from apps.orders.models import Order, OrderStatusLog
from .models import Payment
from .services import close_payment, close_unpaid_payments_for_order
from .wechatpay import WechatPayClient, WechatPaySignatureError


class WechatPayClientTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        temp_path = Path(self.temp_dir.name)

        self.merchant_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        merchant_private_path = temp_path / 'merchant_private.pem'
        merchant_private_path.write_bytes(
            self.merchant_private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )

        self.wechat_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        wechat_public_path = temp_path / 'wechat_public.pem'
        wechat_public_path.write_bytes(
            self.wechat_private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )

        self.settings_override = self.settings(
            WECHAT_APP_ID='wx_test_appid',
            WECHATPAY_MCH_ID='1900000001',
            WECHATPAY_MERCHANT_SERIAL_NO='MERCHANT_SERIAL_TEST',
            WECHATPAY_MERCHANT_PRIVATE_KEY_PATH=str(merchant_private_path),
            WECHATPAY_API_V3_KEY='0123456789abcdef0123456789abcdef',
            WECHATPAY_PUBLIC_KEY_ID='PUB_KEY_ID_3000000001',
            WECHATPAY_PUBLIC_KEY_PATH=str(wechat_public_path),
            WECHATPAY_NOTIFY_URL='https://example.com/api/pay/wechat/callback',
            WECHATPAY_HTTP_TIMEOUT=10,
            WECHATPAY_TIMESTAMP_TOLERANCE_SECONDS=300,
        )
        self.settings_override.enable()
        self.client = WechatPayClient()

    def tearDown(self):
        self.settings_override.disable()
        self.temp_dir.cleanup()

    def test_build_miniprogram_payment_params_signature(self):
        params = self.client.build_miniprogram_payment_params('wx_prepay_test')
        message = (
            f"wx_test_appid\n{params['timeStamp']}\n{params['nonceStr']}\n"
            f"{params['package']}\n"
        )
        self.merchant_private_key.public_key().verify(
            base64.b64decode(params['paySign']),
            message.encode('utf-8'),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        self.assertEqual(params['signType'], 'RSA')

    def test_verify_and_decrypt_callback(self):
        transaction = {
            'appid': 'wx_test_appid',
            'mchid': '1900000001',
            'out_trade_no': 'PAY20260612000000000001',
            'transaction_id': '42000000000000000001',
            'trade_state': 'SUCCESS',
            'amount': {'total': 100, 'currency': 'CNY'},
        }
        nonce = '0123456789ab'
        associated_data = 'transaction'
        ciphertext = AESGCM(b'0123456789abcdef0123456789abcdef').encrypt(
            nonce.encode('utf-8'),
            json.dumps(transaction, separators=(',', ':')).encode('utf-8'),
            associated_data.encode('utf-8'),
        )
        envelope = {
            'id': 'notification-test',
            'event_type': 'TRANSACTION.SUCCESS',
            'resource': {
                'algorithm': 'AEAD_AES_256_GCM',
                'ciphertext': base64.b64encode(ciphertext).decode('ascii'),
                'associated_data': associated_data,
                'nonce': nonce,
            },
        }
        raw_body = json.dumps(envelope, separators=(',', ':'))
        timestamp = str(int(time.time()))
        signature_nonce = 'callback-nonce'
        signature = self.wechat_private_key.sign(
            f'{timestamp}\n{signature_nonce}\n{raw_body}\n'.encode('utf-8'),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        headers = {
            'Wechatpay-Serial': 'PUB_KEY_ID_3000000001',
            'Wechatpay-Timestamp': timestamp,
            'Wechatpay-Nonce': signature_nonce,
            'Wechatpay-Signature': base64.b64encode(signature).decode('ascii'),
        }

        verified = self.client.verify_callback(headers, raw_body)
        self.assertEqual(self.client.decrypt_resource(verified['resource']), transaction)

    def test_rejects_invalid_callback_signature(self):
        headers = {
            'Wechatpay-Serial': 'PUB_KEY_ID_3000000001',
            'Wechatpay-Timestamp': str(int(time.time())),
            'Wechatpay-Nonce': 'nonce',
            'Wechatpay-Signature': base64.b64encode(b'invalid').decode('ascii'),
        }
        with self.assertRaises(WechatPaySignatureError):
            self.client.verify_callback(headers, '{}')


class PaymentCloseFlowTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(username='boss', password='pass')
        self.package = Package.objects.create(name='测试套餐', player_count=1, base_price=100)

    def create_order(self, status=Order.STATUS_WAITING, order_no='ORDER001'):
        return Order.objects.create(
            order_no=order_no,
            boss_user=self.user,
            boss_wechat='boss_wechat',
            package=self.package,
            required_players=1,
            total_price_per_hour=100,
            total_amount=100,
            status=status,
        )

    def create_payment(self, order, status='paying', payment_no='PAY001'):
        return Payment.objects.create(
            payment_no=payment_no,
            order=order,
            channel='wechat',
            scene='jsapi',
            amount=100,
            status=status,
            third_order_no='prepay_test',
            expires_at=timezone.now(),
        )

    @override_settings(ENABLE_MOCK_PAYMENT=True)
    def test_close_payment_marks_local_payment_closed_in_mock_mode(self):
        order = self.create_order()
        payment = self.create_payment(order)

        close_payment(payment, reason='测试关单')

        payment.refresh_from_db()
        self.assertEqual(payment.status, 'closed')
        self.assertEqual(payment.notify_payload['close']['reason'], '测试关单')
        self.assertFalse(payment.notify_payload['close']['wechat_closed'])

    @override_settings(ENABLE_MOCK_PAYMENT=False)
    def test_close_payment_calls_wechat_close_order_in_real_mode(self):
        order = self.create_order()
        payment = self.create_payment(order)

        with patch('apps.payments.services.WechatPayClient') as client_cls:
            client_cls.return_value.close_order.return_value = {}
            close_payment(payment, reason='真实关单测试')

        client_cls.return_value.close_order.assert_called_once_with(payment.payment_no)
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'closed')
        self.assertTrue(payment.notify_payload['close']['wechat_closed'])

    @override_settings(ENABLE_MOCK_PAYMENT=True)
    def test_cancel_order_view_closes_unpaid_payment(self):
        order = self.create_order(status=Order.STATUS_WAITING)
        payment = self.create_payment(order)
        request = self.factory.post('/api/boss/orders/ORDER001/cancel', {'reason': '老板取消'}, format='json')
        force_authenticate(request, user=self.user)

        response = cancel_order_view(request, order.order_no)

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        self.assertEqual(payment.status, 'closed')

    @override_settings(ENABLE_MOCK_PAYMENT=True)
    def test_close_unpaid_payments_does_not_close_paid_payment(self):
        order = self.create_order()
        paying = self.create_payment(order, payment_no='PAY001', status='paying')
        paid = self.create_payment(order, payment_no='PAY002', status='paid')

        close_unpaid_payments_for_order(order, reason='批量关单')

        paying.refresh_from_db()
        paid.refresh_from_db()
        self.assertEqual(paying.status, 'closed')
        self.assertEqual(paid.status, 'paid')

    @override_settings(ENABLE_MOCK_PAYMENT=True)
    def test_manual_payment_confirmation_marks_order_completed_and_logs_status(self):
        order = self.create_order(status=Order.STATUS_PENDING_PAYMENT)
        request = self.factory.post('/api/boss/orders/ORDER001/self-confirm-payment', {'actual_amount': 120}, format='json')
        force_authenticate(request, user=self.user)

        response = self_confirm_payment(request, order.order_no)

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertTrue(order.paid)
        self.assertEqual(order.status, Order.STATUS_COMPLETED)
        self.assertEqual(order.payment_method, 'self_confirm')
        self.assertEqual(order.total_amount, 120)
        self.assertTrue(
            OrderStatusLog.objects.filter(
                order=order,
                from_status=Order.STATUS_PENDING_PAYMENT,
                to_status=Order.STATUS_COMPLETED,
                reason='手动确认支付',
            ).exists()
        )
