import json
import logging
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache

from .virtualpay import (
    TOKEN_INVALID_CODES,
    VirtualPaymentAPIError,
    _access_token_cache_key,
    compact_json,
    get_access_token,
    hmac_sha256_hex,
)


logger = logging.getLogger(__name__)
IDEMPOTENT_SUCCESS_CODES = {268490004}


def _user_xpay_post_once(endpoint, body, pay_sig, signature, access_token):
    query = urlencode({
        'access_token': access_token,
        'pay_sig': pay_sig,
        'signature': signature,
    })
    request = Request(
        f'https://api.weixin.qq.com{endpoint}?{query}',
        data=body.encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=settings.WECHAT_VIRTUALPAY_HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as exc:
        raise VirtualPaymentAPIError('network_error', '微信虚拟支付用户态接口暂不可用') from exc


def user_xpay_post(endpoint, payload, session_key, *, idempotent_success=True):
    """Call an XPay API that requires both AppKey and session_key signatures.

    query_user_balance/currency_pay/cancel_currency_pay sign the exact compact
    JSON request body with AppKey (pay_sig) and the current session_key
    (signature).  Duplicate-operation code 268490004 is treated as success for
    idempotent payment/refund retries.
    """
    if not session_key:
        raise VirtualPaymentAPIError('session_key_missing', '微信登录态已失效，请重新进入小程序后重试')

    body = compact_json(payload)
    pay_sig = hmac_sha256_hex(settings.WECHAT_VIRTUALPAY_APP_KEY, f'{endpoint}&{body}')
    signature = hmac_sha256_hex(session_key, body)

    data = _user_xpay_post_once(endpoint, body, pay_sig, signature, get_access_token())
    errcode = int(data.get('errcode') or 0)
    if errcode in TOKEN_INVALID_CODES:
        cache.delete(_access_token_cache_key())
        data = _user_xpay_post_once(
            endpoint,
            body,
            pay_sig,
            signature,
            get_access_token(force_refresh=True),
        )
        errcode = int(data.get('errcode') or 0)

    if errcode == 0 or (idempotent_success and errcode in IDEMPOTENT_SUCCESS_CODES):
        return data

    logger.error(
        '[虚拟支付用户态] 接口失败 endpoint=%s order_id=%s errcode=%s errmsg=%s',
        endpoint,
        payload.get('order_id', ''),
        data.get('errcode'),
        data.get('errmsg'),
    )
    raise VirtualPaymentAPIError(data.get('errcode'), data.get('errmsg') or '微信虚拟支付用户态接口调用失败')
