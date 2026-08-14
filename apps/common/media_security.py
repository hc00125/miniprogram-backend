from rest_framework.exceptions import ValidationError

from .content_security import MEDIA_TYPE_AUDIO, content_security_enabled
from .models import MediaContentSecurityCheck


STATUS_BY_SUGGEST = {
    'pass': MediaContentSecurityCheck.STATUS_PASS,
    'review': MediaContentSecurityCheck.STATUS_REVIEW,
    'risky': MediaContentSecurityCheck.STATUS_RISKY,
}


def register_media_check(*, user, media_url, media_type, scene, trace_id):
    trace = str(trace_id or '').strip()
    url = str(media_url or '').strip()
    if not trace or not url or not content_security_enabled():
        return None
    return MediaContentSecurityCheck.objects.create(
        user=user,
        trace_id=trace,
        media_url=url,
        media_type=int(media_type),
        scene=int(scene),
        status=MediaContentSecurityCheck.STATUS_PENDING,
    )


def latest_media_check(*, user, media_url, media_type=MEDIA_TYPE_AUDIO):
    url = str(media_url or '').strip()
    if not url or not user or not getattr(user, 'is_authenticated', False):
        return None
    return (
        MediaContentSecurityCheck.objects
        .filter(user=user, media_url=url, media_type=int(media_type))
        .order_by('-created_at', '-id')
        .first()
    )


def ensure_tracked_media_reference(*, user, media_url, media_type=MEDIA_TYPE_AUDIO):
    """Reject arbitrary user-supplied media URLs that bypass the upload/check endpoint.

    A pending async result may still be attached to an application because it is not public yet.
    Publication is guarded separately by ``ensure_media_publishable``.
    """
    url = str(media_url or '').strip()
    if not url or not content_security_enabled():
        return None

    check = latest_media_check(user=user, media_url=url, media_type=media_type)
    if not check:
        raise ValidationError({'detail': '语音必须通过平台上传接口完成内容安全检测后提交'})
    if check.status in {
        MediaContentSecurityCheck.STATUS_REVIEW,
        MediaContentSecurityCheck.STATUS_RISKY,
        MediaContentSecurityCheck.STATUS_ERROR,
    }:
        raise ValidationError({'detail': '语音内容安全检测未通过，请重新上传'})
    return check


def ensure_media_publishable(*, user, media_url, media_type=MEDIA_TYPE_AUDIO):
    """Require an async WeChat media check to have returned ``pass`` before publication."""
    url = str(media_url or '').strip()
    if not url or not content_security_enabled():
        return None

    check = latest_media_check(user=user, media_url=url, media_type=media_type)
    if not check:
        raise ValidationError('语音缺少微信内容安全检测记录，不能公开')
    if check.status == MediaContentSecurityCheck.STATUS_PENDING:
        raise ValidationError('语音内容安全检测尚未完成，请稍后再审核')
    if check.status != MediaContentSecurityCheck.STATUS_PASS:
        raise ValidationError('语音内容安全检测未通过，不能公开')
    return check


def apply_media_check_result(*, trace_id, suggest='', label='', errcode=0, errmsg='', raw_result=None):
    trace = str(trace_id or '').strip()
    if not trace:
        return None
    check = MediaContentSecurityCheck.objects.filter(trace_id=trace).first()
    if not check:
        return None

    normalized_suggest = str(suggest or '').strip().lower()
    try:
        normalized_errcode = int(errcode or 0)
    except (TypeError, ValueError):
        normalized_errcode = -1

    if normalized_errcode != 0:
        status = MediaContentSecurityCheck.STATUS_ERROR
    else:
        status = STATUS_BY_SUGGEST.get(normalized_suggest, MediaContentSecurityCheck.STATUS_ERROR)

    check.status = status
    check.suggest = normalized_suggest
    check.label = str(label or '')[:64]
    check.errcode = normalized_errcode
    check.errmsg = str(errmsg or '')[:300]
    check.raw_result = raw_result if isinstance(raw_result, dict) else {}
    check.save(update_fields=[
        'status', 'suggest', 'label', 'errcode', 'errmsg', 'raw_result', 'updated_at',
    ])
    return check
