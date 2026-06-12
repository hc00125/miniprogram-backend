import base64
import binascii
import json
import secrets
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import requests
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings


class WechatPayError(Exception):
    """Base exception for WeChat Pay integration errors."""


class WechatPayConfigurationError(WechatPayError):
    """Raised when required WeChat Pay settings are missing or invalid."""


class WechatPaySignatureError(WechatPayError):
    """Raised when a response or callback signature cannot be verified."""


class WechatPayAPIError(WechatPayError):
    def __init__(self, status_code, code='', message='', response_body=''):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.response_body = response_body
        detail = message or response_body or '微信支付接口调用失败'
        if code:
            detail = f'{code}: {detail}'
        super().__init__(detail)


def _resolve_path(raw_path):
    if not raw_path:
        raise WechatPayConfigurationError('微信支付密钥文件路径未配置')
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path(settings.BASE_DIR) / path
    if not path.exists() or not path.is_file():
        raise WechatPayConfigurationError(f'微信支付密钥文件不存在: {path}')
    return path.resolve()


@lru_cache(maxsize=8)
def _load_private_key(path_text):
    data = Path(path_text).read_bytes()
    try:
        return serialization.load_pem_private_key(data, password=None)
    except (TypeError, ValueError) as exc:
        raise WechatPayConfigurationError('商户API私钥文件格式不正确') from exc


@lru_cache(maxsize=8)
def _load_public_key(path_text):
    data = Path(path_text).read_bytes()
    try:
        return serialization.load_pem_public_key(data)
    except (TypeError, ValueError) as exc:
        raise WechatPayConfigurationError('微信支付公钥文件格式不正确') from exc


class WechatPayClient:
    API_HOST = 'https://api.mch.weixin.qq.com'

    def __init__(self):
        self.appid = settings.WECHAT_APP_ID
        self.mchid = settings.WECHATPAY_MCH_ID
        self.merchant_serial_no = settings.WECHATPAY_MERCHANT_SERIAL_NO
        self.api_v3_key = settings.WECHATPAY_API_V3_KEY
        self.public_key_id = settings.WECHATPAY_PUBLIC_KEY_ID
        self.notify_url = settings.WECHATPAY_NOTIFY_URL
        self.timeout = settings.WECHATPAY_HTTP_TIMEOUT
        self.timestamp_tolerance = settings.WECHATPAY_TIMESTAMP_TOLERANCE_SECONDS

        required = {
            'WECHAT_APP_ID': self.appid,
            'WECHATPAY_MCH_ID': self.mchid,
            'WECHATPAY_MERCHANT_SERIAL_NO': self.merchant_serial_no,
            'WECHATPAY_API_V3_KEY': self.api_v3_key,
            'WECHATPAY_PUBLIC_KEY_ID': self.public_key_id,
            'WECHATPAY_NOTIFY_URL': self.notify_url,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise WechatPayConfigurationError(f'缺少微信支付配置: {", ".join(missing)}')
        if not self.public_key_id.startswith('PUB_KEY_ID_'):
            raise WechatPayConfigurationError('WECHATPAY_PUBLIC_KEY_ID 格式不正确')
        if len(self.api_v3_key.encode('utf-8')) != 32:
            raise WechatPayConfigurationError('WECHATPAY_API_V3_KEY 必须是32字节')

        private_key_path = _resolve_path(settings.WECHATPAY_MERCHANT_PRIVATE_KEY_PATH)
        public_key_path = _resolve_path(settings.WECHATPAY_PUBLIC_KEY_PATH)
        self.private_key = _load_private_key(str(private_key_path))
        self.public_key = _load_public_key(str(public_key_path))

    def _sign(self, message):
        signature = self.private_key.sign(
            message.encode('utf-8'),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode('ascii')

    def _build_authorization(self, method, canonical_url, body):
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        message = f'{method.upper()}\n{canonical_url}\n{timestamp}\n{nonce}\n{body}\n'
        signature = self._sign(message)
        return (
            'WECHATPAY2-SHA256-RSA2048 '
            f'mchid="{self.mchid}",'
            f'nonce_str="{nonce}",'
            f'timestamp="{timestamp}",'
            f'serial_no="{self.merchant_serial_no}",'
            f'signature="{signature}"'
        )

    @staticmethod
    def _normalized_headers(headers):
        return {str(key).lower(): value for key, value in headers.items()}

    def verify_signature(self, headers, body):
        normalized = self._normalized_headers(headers)
        serial = normalized.get('wechatpay-serial', '')
        timestamp = normalized.get('wechatpay-timestamp', '')
        nonce = normalized.get('wechatpay-nonce', '')
        signature = normalized.get('wechatpay-signature', '')
        if not all([serial, timestamp, nonce, signature]):
            raise WechatPaySignatureError('微信支付签名请求头不完整')
        if serial != self.public_key_id:
            raise WechatPaySignatureError(f'未知的微信支付公钥ID: {serial}')
        try:
            timestamp_int = int(timestamp)
        except ValueError as exc:
            raise WechatPaySignatureError('微信支付时间戳格式不正确') from exc
        if abs(int(time.time()) - timestamp_int) > self.timestamp_tolerance:
            raise WechatPaySignatureError('微信支付签名时间戳已过期')

        message = f'{timestamp}\n{nonce}\n{body}\n'
        try:
            self.public_key.verify(
                base64.b64decode(signature),
                message.encode('utf-8'),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (InvalidSignature, ValueError) as exc:
            raise WechatPaySignatureError('微信支付签名验证失败') from exc
        return True

    def request(self, method, canonical_url, payload=None):
        body = '' if payload is None else json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Wechatpay-Serial': self.public_key_id,
            'Authorization': self._build_authorization(method, canonical_url, body),
            'User-Agent': 'miniprogram-backend/1.0',
        }
        try:
            response = requests.request(
                method=method,
                url=f'{self.API_HOST}{canonical_url}',
                data=body.encode('utf-8') if body else None,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise WechatPayAPIError(503, message='连接微信支付接口失败') from exc

        response_body = response.text
        signature_headers_present = all(
            response.headers.get(name)
            for name in ('Wechatpay-Serial', 'Wechatpay-Timestamp', 'Wechatpay-Nonce', 'Wechatpay-Signature')
        )
        if signature_headers_present:
            self.verify_signature(response.headers, response_body)
        elif response.ok:
            raise WechatPaySignatureError('微信支付响应缺少验签信息')

        if not response.ok:
            try:
                error_data = response.json()
            except ValueError:
                error_data = {}
            raise WechatPayAPIError(
                response.status_code,
                code=error_data.get('code', ''),
                message=error_data.get('message', ''),
                response_body=response_body[:1000],
            )
        try:
            return response.json()
        except ValueError as exc:
            raise WechatPayAPIError(response.status_code, message='微信支付响应不是有效JSON') from exc

    def create_jsapi_order(self, *, out_trade_no, description, amount_total, openid, time_expire, attach=''):
        payload = {
            'appid': self.appid,
            'mchid': self.mchid,
            'description': description[:127],
            'out_trade_no': out_trade_no,
            'time_expire': time_expire,
            'notify_url': self.notify_url,
            'amount': {'total': amount_total, 'currency': 'CNY'},
            'payer': {'openid': openid},
        }
        if attach:
            payload['attach'] = attach[:128]
        return self.request('POST', '/v3/pay/transactions/jsapi', payload)

    def query_order(self, out_trade_no):
        safe_trade_no = quote(out_trade_no, safe='')
        canonical_url = f'/v3/pay/transactions/out-trade-no/{safe_trade_no}?mchid={quote(self.mchid, safe="")}'
        return self.request('GET', canonical_url)

    def build_miniprogram_payment_params(self, prepay_id):
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        package = f'prepay_id={prepay_id}'
        message = f'{self.appid}\n{timestamp}\n{nonce}\n{package}\n'
        return {
            'timeStamp': timestamp,
            'nonceStr': nonce,
            'package': package,
            'signType': 'RSA',
            'paySign': self._sign(message),
        }

    def verify_callback(self, headers, raw_body):
        self.verify_signature(headers, raw_body)
        try:
            return json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise WechatPayError('微信支付回调不是有效JSON') from exc

    def decrypt_resource(self, resource):
        if resource.get('algorithm') != 'AEAD_AES_256_GCM':
            raise WechatPayError('不支持的微信支付回调加密算法')
        try:
            ciphertext = base64.b64decode(resource['ciphertext'])
            nonce = resource['nonce'].encode('utf-8')
            associated_data = resource.get('associated_data', '').encode('utf-8')
            plaintext = AESGCM(self.api_v3_key.encode('utf-8')).decrypt(
                nonce,
                ciphertext,
                associated_data,
            )
            return json.loads(plaintext.decode('utf-8'))
        except (KeyError, ValueError, InvalidTag, binascii.Error, json.JSONDecodeError) as exc:
            raise WechatPayError('微信支付回调解密失败') from exc
