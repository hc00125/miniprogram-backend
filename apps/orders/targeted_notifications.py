"""Best-effort WeChat and SMS notifications for designated orders."""

import json
import logging
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.payments.virtualpay import get_access_token
from apps.players.models import PlayerOrderNoticeSubscription

from .models import Order, OrderDesignation, OrderStatusLog


logger = logging.getLogger(__name__)


def _template_fields():
    try:
        value = json.loads(settings.WECHAT_PLAYER_ORDER_TEMPLATE_FIELDS or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _format_amount(order):
    raw = order.total_amount if order.total_amount is not None else order.total_price_per_hour
    try:
        amount = Decimal(str(raw or 0)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal('0.00')
    return f'{amount:.2f}元'


def _trim_field_value(field, value):
    text = str(value or '')
    if field.startswith('thing'):
        return text[:20]
    if field.startswith('time'):
        return text[:20]
    if field.startswith('amount'):
        return text[:20]
    if field.startswith('character_string'):
        return text[:32]
    return text[:32]


def _render_template_data(order, notification_note='老板已指定您，请尽快确认'):
    context = {
        'package_name': order.package_name_snapshot or order.package.name,
        'spec_name': order.spec_name_snapshot or '',
        'created_at': timezone.localtime(order.created_at).strftime('%Y-%m-%d %H:%M'),
        'boss_contact': order.boss_wechat or '',
        'game_id': order.game_id or '',
        'booked_hours': str(order.booked_hours or 1),
        'order_no': order.order_no,
        'total_amount': _format_amount(order),
        'notification_note': notification_note,
    }
    data = {}
    for field, template in _template_fields().items():
        if not isinstance(field, str) or not isinstance(template, str):
            continue
        try:
            rendered = template.format_map(context)
            data[field] = {'value': _trim_field_value(field, rendered)}
        except (KeyError, ValueError):
            logger.warning('invalid designated-order notification field template: %s', field)
    return data


def _record(order, reason):
    OrderStatusLog.objects.create(
        order=order,
        from_status=order.status,
        to_status=order.status,
        reason=reason[:500],
    )


def _consume_subscription(player, template_id):
    subscription = (
        PlayerOrderNoticeSubscription.objects
        .select_for_update()
        .filter(player=player, template_id=template_id, available_count__gt=0)
        .first()
    )
    if not subscription:
        return False
    subscription.available_count -= 1
    subscription.save(update_fields=['available_count', 'updated_at'])
    return True


def _send_order_notice(order, player, notification_note):
    template_id = (settings.WECHAT_PLAYER_ORDER_TEMPLATE_ID or '').strip()
    data = _render_template_data(order, notification_note=notification_note)
    profile = getattr(getattr(player, 'user', None), 'client_profile', None)
    if not template_id or not data or not profile or not profile.openid:
        _record(order, '指定邀请已生成；陪玩师未配置可用的微信订阅消息，已保留站内待接单提醒')
        return False

    with transaction.atomic():
        if not _consume_subscription(player, template_id):
            _record(order, '指定邀请已生成；陪玩师尚未授权接单订阅消息，已保留站内待接单提醒')
            return False

    payload = {
        'touser': profile.openid,
        'template_id': template_id,
        'page': settings.WECHAT_PLAYER_ORDER_TEMPLATE_PAGE,
        'miniprogram_state': settings.WECHAT_SUBSCRIBE_MESSAGE_MINIPROGRAM_STATE,
        'lang': 'zh_CN',
        'data': data,
    }
    try:
        request = Request(
            f'https://api.weixin.qq.com/cgi-bin/message/subscribe/send?{urlencode({"access_token": get_access_token()})}',
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
            method='POST',
        )
        with urlopen(request, timeout=settings.WECHAT_VIRTUALPAY_HTTP_TIMEOUT) as response:
            result = json.loads(response.read().decode('utf-8'))
    except Exception as exc:  # Network failures must not roll back an order.
        logger.exception('designated-order notification failed order=%s player=%s', order.order_no, player.id)
        _record(order, f'指定邀请已生成；微信通知发送失败，已保留站内待接单提醒：{exc}')
        return False

    if int(result.get('errcode') or 0) != 0:
        _record(order, f'指定邀请已生成；微信通知未送达，已保留站内待接单提醒：{result.get("errmsg") or result.get("errcode")}')
        return False
    _record(order, f'已向陪玩师 {player.name} 发送微信指定订单提醒，并保留站内待接单提醒')
    return True


def notify_designation(designation_id):
    """Send WeChat/SMS notifications for one pending designation."""
    designation = (
        OrderDesignation.objects
        .select_related('order__package', 'player__user__client_profile')
        .filter(pk=designation_id, status=OrderDesignation.STATUS_PENDING)
        .first()
    )
    if not designation:
        return False

    order = designation.order
    if designation.expires_at:
        deadline = timezone.localtime(designation.expires_at).strftime('%H:%M')
        notification_note = f'老板已指定您，请在{deadline}前确认'
    else:
        notification_note = '老板已指定您，请尽快确认'

    wechat_sent = _send_order_notice(order, designation.player, notification_note)

    from .sms_notifications import send_designation_sms

    sms_sent = send_designation_sms(designation)
    if sms_sent is True:
        _record(order, f'已向陪玩师 {designation.player.name} 的绑定手机号发送指定订单短信提醒')
    elif sms_sent is False:
        _record(order, '指定邀请已生成；短信通知发送失败，但不影响站内邀请和微信提醒')

    return bool(wechat_sent or sms_sent)


def notify_paid_targeted_order(order_id):
    """Send the paid targeted-product invitation after its transaction commits."""
    order = (
        Order.objects
        .select_related('target_player')
        .filter(pk=order_id, fulfillment_mode=Order.FULFILLMENT_MODE_TARGETED, paid=True)
        .first()
    )
    if not order or not order.target_player_id:
        return False

    designation = (
        OrderDesignation.objects
        .filter(
            order=order,
            player_id=order.target_player_id,
            status=OrderDesignation.STATUS_PENDING,
        )
        .order_by('-id')
        .first()
    )
    if not designation:
        return False
    return notify_designation(designation.id)
