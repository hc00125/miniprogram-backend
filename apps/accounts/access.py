from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException

from .models import AccountRestrictionLog, ClientProfile


class AccountRestricted(APIException):
    status_code = 423
    default_detail = '账户当前无法执行该操作'
    default_code = 'account_restricted'


def refresh_expired_account_restriction(profile):
    """暂停期限届满后自动恢复账户，并留下系统审计记录。"""
    if (
        profile.account_status != ClientProfile.ACCOUNT_STATUS_SUSPENDED
        or not profile.account_suspended_until
        or profile.account_suspended_until > timezone.now()
    ):
        return profile

    with transaction.atomic():
        locked = ClientProfile.objects.select_for_update().get(pk=profile.pk)
        if (
            locked.account_status == ClientProfile.ACCOUNT_STATUS_SUSPENDED
            and locked.account_suspended_until
            and locked.account_suspended_until <= timezone.now()
        ):
            previous_status = locked.account_status
            previous_until = locked.account_suspended_until
            locked.account_status = ClientProfile.ACCOUNT_STATUS_ACTIVE
            locked.account_suspended_until = None
            locked.account_restriction_reason = ''
            locked.account_restricted_at = timezone.now()
            locked.account_restricted_by = None
            locked.save(update_fields=[
                'account_status', 'account_suspended_until',
                'account_restriction_reason', 'account_restricted_at',
                'account_restricted_by', 'updated_at',
            ])
            AccountRestrictionLog.objects.create(
                profile=locked,
                from_status=previous_status,
                to_status=ClientProfile.ACCOUNT_STATUS_ACTIVE,
                suspended_until=previous_until,
                reason='暂停期限届满，系统自动解除账户限制',
                operator=None,
            )
        profile.refresh_from_db(fields=[
            'account_status', 'account_suspended_until',
            'account_restriction_reason', 'account_restricted_at',
            'account_restricted_by',
        ])
    return profile


def account_restriction_payload(user):
    """返回当前有效限制；管理员和没有客户资料的历史账号不受影响。"""
    if not user or not getattr(user, 'is_authenticated', False) or getattr(user, 'is_staff', False):
        return None
    profile = getattr(user, 'client_profile', None)
    if not profile:
        return None

    refresh_expired_account_restriction(profile)
    if profile.account_status == ClientProfile.ACCOUNT_STATUS_ACTIVE:
        return None

    reason = (profile.account_restriction_reason or '').strip()
    if profile.account_status == ClientProfile.ACCOUNT_STATUS_BANNED:
        detail = '您的账户已被永久封禁'
        code = 'ACCOUNT_BANNED'
    else:
        code = 'ACCOUNT_SUSPENDED'
        if profile.account_suspended_until:
            until_text = timezone.localtime(profile.account_suspended_until).strftime('%Y-%m-%d %H:%M')
            detail = f'您的账户已暂停使用至 {until_text}'
        else:
            detail = '您的账户当前处于暂停使用状态'

    if reason:
        detail += f'；原因：{reason}'
    detail += '。如有疑问请联系客服'
    return {
        'detail': detail,
        'code': code,
        'account_status': profile.account_status,
        'reason': reason,
        'suspended_until': profile.account_suspended_until,
    }


def ensure_account_operational(user):
    payload = account_restriction_payload(user)
    if payload:
        raise AccountRestricted(payload)
