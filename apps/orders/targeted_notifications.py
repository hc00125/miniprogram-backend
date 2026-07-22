"""Best-effort WeChat subscription notifications for paid targeted orders."""

import json
import logging
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.payments.virtualpay import get_access_token
from apps.players.models import PlayerOrderNoticeSubscription

from .models import Order, OrderStatusLog


logger = logging.getLogger(__name__)


def _template_fields():
    try:
        value = json.loads(settings.WECHAT_PLAYER_ORDER_TEMPLATE_FIELDS or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _render_template_data(order):
    context = {
        'package_name': order.package_name_snapshot or order.package.name,
        'spec_name': order.spec_name_snapshot or '',
        'created_at': timezone.localtime(order.created_at).strftime('%Y-%m-%d %H:%M'),
        'boss_contact': order.boss_wechat or '',
        'game_id': order.game_id or '',
        'booked_hours': str(order.booked_hours or 1),
        'order_no': order.order_no,
    }
    data = {}
    for field, template in _template_fields().items():
        if not isinstance(field, str) or not isinstance(template, str):
            continue
        try:
            data[field] = {'value': str(template.format_map(context))[:32]}
        except (KeyError, ValueError):
            logger.warning('invalid targeted-order notification field template: %s', field)
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


def notify_paid_targeted_order(order_id):
    """Send one notification after the transaction that records payment commits.

    The notification is never used as the only delivery mechanism: the pending
    invitation remains in the player order list when a player has not granted a
    subscription-message permission or WeChat rejects the message.
    """
    order = (
        Order.objects
        .select_related('package', 'target_player__user__client_profile')
        .filter(pk=order_id, fulfillment_mode=Order.FULFILLMENT_MODE_TARGETED, paid=True)
        .first()
    )
    if not order or not order.target_player_id:
        return False

    template_id = (settings.WECHAT_PLAYER_ORDER_TEMPLATE_ID or '').strip()
    data = _render_template_data(order)
    profile = getattr(getattr(order.target_player, 'user', None), 'client_profile', None)
    if not template_id or not data or not profile or not profile.openid:
        _record(order, '指定服务邀请已生成；陪玩师未配置可用的微信订阅消息，已保留站内待接单提醒')
        return False

    with transaction.atomic():
        if not _consume_subscription(order.target_player, template_id):
            _record(order, '指定服务邀请已生成；陪玩师尚未授权接单订阅消息，已保留站内待接单提醒')
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
    except Exception as exc:  # Network failures must not roll back a paid order.
        logger.exception('targeted-order notification failed order=%s', order.order_no)
        _record(order, f'指定服务邀请已生成；微信通知发送失败，已保留站内待接单提醒：{exc}')
        return False

    if int(result.get('errcode') or 0) != 0:
        _record(order, f'指定服务邀请已生成；微信通知未送达，已保留站内待接单提醒：{result.get("errmsg") or result.get("errcode")}')
        return False
    _record(order, '指定服务邀请已发送微信订阅消息，并保留站内待接单提醒')
    return True
