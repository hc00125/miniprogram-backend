import base64
import hashlib
import json
import logging
import struct
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .media_security import apply_media_check_result


logger = logging.getLogger(__name__)


def _sha1_signature(*parts):
    values = sorted(str(part or '') for part in parts)
    return hashlib.sha1(''.join(values).encode('utf-8')).hexdigest()


def _message_token():
    return str(getattr(settings, 'WECHAT_MESSAGE_TOKEN', '') or '').strip()


def _encoding_aes_key():
    return str(getattr(settings, 'WECHAT_MESSAGE_ENCODING_AES_KEY', '') or '').strip()


def _verify_plain_signature(request):
    token = _message_token()
    if not token:
        return False
    timestamp = request.GET.get('timestamp', '')
    nonce = request.GET.get('nonce', '')
    signature = request.GET.get('signature', '')
    return bool(signature) and _sha1_signature(token, timestamp, nonce) == signature


def _verify_aes_signature(request, encrypted):
    token = _message_token()
    if not token or not encrypted:
        return False
    timestamp = request.GET.get('timestamp', '')
    nonce = request.GET.get('nonce', '')
    signature = request.GET.get('msg_signature', '') or request.GET.get('signature', '')
    return bool(signature) and _sha1_signature(token, timestamp, nonce, encrypted) == signature


def _decrypt_wechat_message(encrypted):
    encoding_key = _encoding_aes_key()
    if len(encoding_key) != 43:
        raise ValueError('WECHAT_MESSAGE_ENCODING_AES_KEY 必须为 43 个字符')

    aes_key = base64.b64decode(f'{encoding_key}=')
    ciphertext = base64.b64decode(encrypted)
    decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(aes_key[:16])).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        raise ValueError('微信消息解密结果为空')

    pad_len = padded[-1]
    if pad_len < 1 or pad_len > 32 or padded[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError('微信消息 PKCS#7 填充无效')
    plain = padded[:-pad_len]
    if len(plain) < 20:
        raise ValueError('微信消息解密结果长度无效')

    message_length = struct.unpack('!I', plain[16:20])[0]
    message_end = 20 + message_length
    if message_end > len(plain):
        raise ValueError('微信消息正文长度无效')

    message = plain[20:message_end]
    appid = plain[message_end:].decode('utf-8')
    expected_appid = str(getattr(settings, 'WECHAT_APP_ID', '') or '')
    if expected_appid and appid != expected_appid:
        raise ValueError('微信消息 AppID 不匹配')
    return message


def _xml_to_dict(element):
    children = list(element)
    if not children:
        return element.text or ''

    result = {}
    for child in children:
        value = _xml_to_dict(child)
        if child.tag in result:
            if not isinstance(result[child.tag], list):
                result[child.tag] = [result[child.tag]]
            result[child.tag].append(value)
        else:
            result[child.tag] = value
    return result


def _parse_payload(raw_body, content_type=''):
    body = bytes(raw_body or b'').strip()
    if not body:
        return {}

    if 'json' in str(content_type or '').lower() or body.startswith(b'{'):
        parsed = json.loads(body.decode('utf-8'))
        return parsed if isinstance(parsed, dict) else {}

    root = ET.fromstring(body)
    parsed = _xml_to_dict(root)
    return parsed if isinstance(parsed, dict) else {}


def _pick(mapping, *keys, default=''):
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _transport_encrypted_value(payload):
    return str(_pick(payload, 'Encrypt', 'encrypt', default='') or '').strip()


def _normalized_callback_payload(payload):
    result = _pick(payload, 'result', 'Result', default={})
    if not isinstance(result, dict):
        result = {}
    return {
        'event': str(_pick(payload, 'Event', 'event', default='') or '').strip(),
        'appid': str(_pick(payload, 'appid', 'Appid', 'AppId', default='') or '').strip(),
        'trace_id': str(_pick(payload, 'trace_id', 'TraceId', 'TraceID', default='') or '').strip(),
        'suggest': str(_pick(result, 'suggest', 'Suggest', default='') or '').strip().lower(),
        'label': _pick(result, 'label', 'Label', default=''),
        'errcode': _pick(payload, 'errcode', 'ErrCode', default=0),
        'errmsg': str(_pick(payload, 'errmsg', 'ErrMsg', default='') or '').strip(),
    }


def _verified_post_payload(request):
    transport_payload = _parse_payload(request.body, request.content_type)
    encrypted = _transport_encrypted_value(transport_payload)
    if encrypted or request.GET.get('encrypt_type') == 'aes':
        if not _verify_aes_signature(request, encrypted):
            raise PermissionError('微信回调签名校验失败')
        decrypted = _decrypt_wechat_message(encrypted)
        return _parse_payload(decrypted, '')

    if not _verify_plain_signature(request):
        raise PermissionError('微信回调签名校验失败')
    return transport_payload


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def content_security_callback(request):
    """Receive Mini Program message-push events, including ``wxa_media_check``.

    Configure this URL in the Mini Program message-push settings. Both plaintext and
    AES encrypted message modes are accepted. Unknown event types are acknowledged
    without side effects so one callback URL can safely receive additional events.
    """
    if request.method == 'GET':
        echostr = request.GET.get('echostr', '')
        if request.GET.get('encrypt_type') == 'aes' or request.GET.get('msg_signature'):
            if not _verify_aes_signature(request, echostr):
                return HttpResponse('invalid signature', status=403)
            try:
                plain_echo = _decrypt_wechat_message(echostr).decode('utf-8')
            except Exception:
                logger.exception('failed to decrypt WeChat callback verification echo')
                return HttpResponse('invalid encrypted echo', status=400)
            return HttpResponse(plain_echo)

        if not _verify_plain_signature(request):
            return HttpResponse('invalid signature', status=403)
        return HttpResponse(echostr)

    try:
        payload = _verified_post_payload(request)
    except PermissionError:
        return HttpResponse('invalid signature', status=403)
    except Exception:
        logger.exception('failed to parse/decrypt WeChat message callback')
        return HttpResponse('invalid payload', status=400)

    normalized = _normalized_callback_payload(payload)
    if normalized['event'] != 'wxa_media_check':
        return HttpResponse('success')

    expected_appid = str(getattr(settings, 'WECHAT_APP_ID', '') or '').strip()
    if normalized['appid'] and expected_appid and normalized['appid'] != expected_appid:
        logger.warning('ignored media security callback with mismatched appid=%s', normalized['appid'])
        return HttpResponse('invalid appid', status=403)

    if not normalized['trace_id']:
        logger.warning('received wxa_media_check callback without trace_id')
        return HttpResponse('success')

    check = apply_media_check_result(
        trace_id=normalized['trace_id'],
        suggest=normalized['suggest'],
        label=normalized['label'],
        errcode=normalized['errcode'],
        errmsg=normalized['errmsg'],
        raw_result=payload,
    )
    if not check:
        logger.warning('received unknown wxa_media_check trace_id=%s', normalized['trace_id'])
    return HttpResponse('success')
