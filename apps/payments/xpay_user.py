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


def user_xpay_post(
    endpoint,
    payload,
    session_key,
    *,
    idempotent_success=True,
    extra_success_codes=None,
):
    """Call an XPay API requiring both AppKey and session_key signatures.

    268490004 is the generic duplicate-operation success code.  Some endpoints
    expose additional state-specific codes that are only safe to accept after
    the caller has validated its own protocol.  Callers may pass those via
    ``extra_success_codes`` instead of weakening every XPay request globally.
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

    accepted_codes = set(extra_success_codes or ())
    if idempotent_success:
        accepted_codes.update(IDEMPOTENT_SUCCESS_CODES)
    if errcode == 0 or errcode in accepted_codes:
        return data

    logger.error(
        '[虚拟支付用户态] 接口失败 endpoint=%s order_id=%s errcode=%s errmsg=%s',
        endpoint,
        payload.get('order_id', ''),
        data.get('errcode'),
        data.get('errmsg'),
    )
    raise VirtualPaymentAPIError(data.get('errcode'), data.get('errmsg') or '微信虚拟支付用户态接口调用失败')
