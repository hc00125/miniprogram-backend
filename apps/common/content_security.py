import json
import logging
import uuid
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache
from rest_framework.exceptions import ValidationError


logger = logging.getLogger(__name__)

TOKEN_INVALID_CODES = {40001, 40014, 42001}
TEXT_REJECT_CODES = {87014}
IMAGE_REJECT_CODES = {87014}

SCENE_PROFILE = 1
SCENE_COMMENT = 2
SCENE_FORUM = 3
SCENE_SOCIAL = 4


class ContentSecurityError(Exception):
    pass


class ContentSecurityRejected(ContentSecurityError):
    pass


class ContentSecurityUnavailable(ContentSecurityError):
    pass


def content_security_enabled():
    return bool(getattr(settings, 'WECHAT_CONTENT_SECURITY_ENABLED', False))


def _timeout():
    return float(getattr(settings, 'WECHAT_CONTENT_SECURITY_HTTP_TIMEOUT', 8))


def _token_cache_key():
    return f'wechat-stable-access-token:{settings.WECHAT_APP_ID}'


def _fetch_access_token(force_refresh=False):
    if not settings.WECHAT_APP_ID or not settings.WECHAT_APP_SECRET:
        raise ContentSecurityUnavailable('微信小程序 AppID/AppSecret 未配置')

    body = json.dumps({
        'grant_type': 'client_credential',
        'appid': settings.WECHAT_APP_ID,
        'secret': settings.WECHAT_APP_SECRET,
        'force_refresh': bool(force_refresh),
    }, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    request = Request(
        'https://api.weixin.qq.com/cgi-bin/stable_token',
        data=body,
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=_timeout()) as response:
            data = json.loads(response.read().decode('utf-8'))
    except Exception as exc:
        raise ContentSecurityUnavailable('获取微信内容安全接口凭证失败') from exc

    token = data.get('access_token')
    if not token:
        raise ContentSecurityUnavailable(data.get('errmsg') or '获取微信内容安全接口凭证失败')
    return token, int(data.get('expires_in') or 7200)


def get_access_token(force_refresh=False):
    cache_key = _token_cache_key()
    if not force_refresh:
        cached = cache.get(cache_key)
        if cached:
            return cached

    token, expires_in = _fetch_access_token(force_refresh=force_refresh)
    cache.set(cache_key, token, max(60, expires_in - 300))
    return token


def _decode_response(response):
    try:
        return json.loads(response.read().decode('utf-8'))
    except Exception as exc:
        raise ContentSecurityUnavailable('微信内容安全接口返回异常') from exc


def _post_json_once(endpoint, payload, *, force_refresh=False):
    token = get_access_token(force_refresh=force_refresh)
    body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    request = Request(
        f'https://api.weixin.qq.com{endpoint}?{urlencode({"access_token": token})}',
        data=body,
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=_timeout()) as response:
            return _decode_response(response)
    except ContentSecurityUnavailable:
        raise
    except Exception as exc:
        raise ContentSecurityUnavailable('微信内容安全接口暂不可用') from exc


def _post_json(endpoint, payload):
    data = _post_json_once(endpoint, payload)
    errcode = int(data.get('errcode') or 0)
    if errcode in TOKEN_INVALID_CODES:
        cache.delete(_token_cache_key())
        data = _post_json_once(endpoint, payload, force_refresh=True)
    return data


def _multipart_body(file_bytes, *, filename, content_type):
    boundary = f'----TouchiContentSecurity{uuid.uuid4().hex}'
    header = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="media"; filename="{filename}"\r\n'
        f'Content-Type: {content_type}\r\n\r\n'
    ).encode('utf-8')
    footer = f'\r\n--{boundary}--\r\n'.encode('utf-8')
    return boundary, header + file_bytes + footer


def _post_image_once(file_bytes, *, filename, content_type, force_refresh=False):
    token = get_access_token(force_refresh=force_refresh)
    boundary, body = _multipart_body(file_bytes, filename=filename, content_type=content_type)
    request = Request(
        f'https://api.weixin.qq.com/wxa/img_sec_check?{urlencode({"access_token": token})}',
        data=body,
        headers={
            'Content-Type': f'multipart/form-data; boundary={boundary}',
            'Accept': 'application/json',
        },
        method='POST',
    )
    try:
        with urlopen(request, timeout=_timeout()) as response:
            return _decode_response(response)
    except ContentSecurityUnavailable:
        raise
    except Exception as exc:
        raise ContentSecurityUnavailable('微信图片安全检测暂不可用') from exc


def _post_image(file_bytes, *, filename, content_type):
    data = _post_image_once(file_bytes, filename=filename, content_type=content_type)
    errcode = int(data.get('errcode') or 0)
    if errcode in TOKEN_INVALID_CODES:
        cache.delete(_token_cache_key())
        data = _post_image_once(
            file_bytes,
            filename=filename,
            content_type=content_type,
            force_refresh=True,
        )
    return data


def check_text(content, *, openid, scene=SCENE_SOCIAL):
    text = str(content or '').strip()
    if not text or not content_security_enabled():
        return None
    if not openid:
        raise ContentSecurityUnavailable('当前账号缺少微信 OpenID，无法完成内容安全检测')

    data = _post_json('/wxa/msg_sec_check', {
        'content': text,
        'version': 2,
        'scene': int(scene),
        'openid': str(openid),
    })
    errcode = int(data.get('errcode') or 0)
    if errcode in TEXT_REJECT_CODES:
        raise ContentSecurityRejected('发布内容含违规信息')
    if errcode != 0:
        logger.warning('msgSecCheck failed errcode=%s errmsg=%s', data.get('errcode'), data.get('errmsg'))
        raise ContentSecurityUnavailable(data.get('errmsg') or '文本内容安全检测失败')

    result = data.get('result') or {}
    suggest = str(result.get('suggest') or '').lower()
    if suggest != 'pass':
        raise ContentSecurityRejected('发布内容含违规信息')
    return data


def check_texts(contents, *, openid, scene=SCENE_SOCIAL):
    parts = [str(value).strip() for value in contents if str(value or '').strip()]
    if not parts:
        return None
    return check_text('\n'.join(parts), openid=openid, scene=scene)


def check_image_file(file_obj, *, openid=None):
    del openid  # 同步图片安全接口不需要额外传 OpenID。
    if not content_security_enabled():
        return None

    original_position = None
    try:
        if hasattr(file_obj, 'tell'):
            original_position = file_obj.tell()
        file_bytes = file_obj.read()
    finally:
        if hasattr(file_obj, 'seek'):
            file_obj.seek(original_position or 0)

    if not file_bytes:
        raise ContentSecurityUnavailable('头像文件为空，无法完成安全检测')

    data = _post_image(
        file_bytes,
        filename=getattr(file_obj, 'name', None) or 'avatar.jpg',
        content_type=getattr(file_obj, 'content_type', None) or 'application/octet-stream',
    )
    errcode = int(data.get('errcode') or 0)
    if errcode in IMAGE_REJECT_CODES:
        raise ContentSecurityRejected('图片含违规内容')
    if errcode != 0:
        logger.warning('imgSecCheck failed errcode=%s errmsg=%s', data.get('errcode'), data.get('errmsg'))
        raise ContentSecurityUnavailable(data.get('errmsg') or '图片内容安全检测失败')
    return data


def _validation_error_for(exc):
    if isinstance(exc, ContentSecurityRejected):
        return ValidationError({'detail': '发布内容含违规信息，请修改后重试'})
    return ValidationError({'detail': '内容安全检测暂时不可用，请稍后重试'})


def ensure_text_safe(content, *, openid, scene=SCENE_SOCIAL):
    try:
        return check_text(content, openid=openid, scene=scene)
    except ContentSecurityError as exc:
        raise _validation_error_for(exc) from exc


def ensure_texts_safe(contents, *, openid, scene=SCENE_SOCIAL):
    try:
        return check_texts(contents, openid=openid, scene=scene)
    except ContentSecurityError as exc:
        raise _validation_error_for(exc) from exc


def ensure_image_safe(file_obj, *, openid=None):
    try:
        return check_image_file(file_obj, openid=openid)
    except ContentSecurityError as exc:
        if isinstance(exc, ContentSecurityRejected):
            raise ValidationError({'detail': '图片含违规内容，请更换后重试'}) from exc
        raise ValidationError({'detail': '内容安全检测暂时不可用，请稍后重试'}) from exc


def user_openid(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return ''
    profile = getattr(user, 'client_profile', None)
    return str(getattr(profile, 'openid', '') or '')
