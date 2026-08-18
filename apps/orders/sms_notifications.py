"""Best-effort Tencent Cloud SMS notifications for designated-player invitations."""

import json
import logging
import re
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils import timezone


logger = logging.getLogger(__name__)


def _template_params():
    try:
        value = json.loads(settings.TENCENT_SMS_TEMPLATE_PARAMS or '[]')
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _format_amount(order):
    raw = order.total_amount if order.total_amount is not None else order.total_price_per_hour
    try:
        amount = Decimal(str(raw or 0)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal('0.00')
    return f'{amount:.2f}'


def _render_params(designation):
    order = designation.order
    deadline = ''
    if designation.expires_at:
        deadline = timezone.localtime(designation.expires_at).strftime('%m-%d %H:%M')
    context = {
        'order_no': str(order.order_no or '')[:32],
        'deadline': deadline[:20],
        'package_name': str(order.package_name_snapshot or getattr(order.package, 'name', '') or '')[:20],
        'spec_name': str(order.spec_name_snapshot or '')[:20],
        'player_name': str(designation.player.name or '')[:20],
        'booked_hours': str(order.booked_hours or 1)[:12],
        'total_amount': _format_amount(order)[:16],
    }
    rendered = []
    for item in _template_params():
        if not isinstance(item, str):
            continue
        try:
            rendered.append(item.format_map(context)[:32])
        except (KeyError, ValueError):
            logger.warning('invalid Tencent SMS template parameter: %s', item)
            rendered.append('')
    return rendered


def _e164_phone(binding):
    phone = re.sub(r'[^0-9+]', '', str(binding.phone_number or ''))
    country_code = re.sub(r'\D', '', str(binding.country_code or '86')) or '86'
    if not phone:
        return ''
    if phone.startswith('+'):
        return phone
    digits = re.sub(r'\D', '', phone)
    if country_code == '86' and digits.startswith('86') and len(digits) > 11:
        digits = digits[2:]
    return f'+{country_code}{digits}'


def _configuration_complete():
    return all([
        settings.TENCENT_SMS_SECRET_ID,
        settings.TENCENT_SMS_SECRET_KEY,
        settings.TENCENT_SMS_SDK_APP_ID,
        settings.TENCENT_SMS_SIGN_NAME,
        settings.TENCENT_SMS_TEMPLATE_ID,
    ])


def send_designation_sms(designation):
    """
    Send one notification SMS when the designated player has bound a phone.

    Returns None when SMS is disabled or the player has no bound phone, True on
    accepted submission, and False when an enabled/configured attempt fails.
    """
    if not settings.TENCENT_SMS_ENABLED:
        return None

    profile = getattr(getattr(designation.player, 'user', None), 'client_profile', None)
    if not profile:
        return None
    binding = getattr(profile, 'phone_binding', None)
    if not binding or not binding.phone_number:
        return None

    if not _configuration_complete():
        logger.warning('Tencent SMS enabled but configuration is incomplete')
        return False

    phone_number = _e164_phone(binding)
    if not phone_number:
        return False

    try:
        from tencentcloud.common import credential
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile
        from tencentcloud.sms.v20210111 import models, sms_client

        cred = credential.Credential(settings.TENCENT_SMS_SECRET_ID, settings.TENCENT_SMS_SECRET_KEY)
        http_profile = HttpProfile()
        http_profile.reqMethod = 'POST'
        http_profile.reqTimeout = settings.TENCENT_SMS_HTTP_TIMEOUT
        http_profile.endpoint = 'sms.tencentcloudapi.com'

        client_profile = ClientProfile()
        client_profile.signMethod = 'TC3-HMAC-SHA256'
        client_profile.httpProfile = http_profile
        client = sms_client.SmsClient(cred, settings.TENCENT_SMS_REGION, client_profile)

        request = models.SendSmsRequest()
        request.PhoneNumberSet = [phone_number]
        request.SmsSdkAppId = settings.TENCENT_SMS_SDK_APP_ID
        request.TemplateId = settings.TENCENT_SMS_TEMPLATE_ID
        request.SignName = settings.TENCENT_SMS_SIGN_NAME
        request.TemplateParamSet = _render_params(designation)
        request.SessionContext = f'designation:{designation.id}'

        response = client.SendSms(request)
        statuses = list(response.SendStatusSet or [])
        if not statuses:
            logger.error('Tencent SMS returned no status designation=%s', designation.id)
            return False
        status = statuses[0]
        if str(status.Code or '') != 'Ok':
            logger.error(
                'Tencent SMS rejected designation=%s code=%s message=%s',
                designation.id,
                status.Code,
                status.Message,
            )
            return False
        return True
    except ImportError:
        logger.exception('Tencent Cloud SDK is not installed')
        return False
    except TencentCloudSDKException:
        logger.exception('Tencent SMS SDK error designation=%s', designation.id)
        return False
    except Exception:
        logger.exception('Tencent SMS send failed designation=%s', designation.id)
        return False
