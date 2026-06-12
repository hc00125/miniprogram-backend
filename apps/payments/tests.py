import base64
import json
import tempfile
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.test import SimpleTestCase

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
