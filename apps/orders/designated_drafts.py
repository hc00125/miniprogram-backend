"""Draft lifecycle for static designated-play composition orders."""

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.catalog.models import PackageSpec

from .composition_pricing import (
    apply_quote_to_order,
    normalize_designated_player_ids,
    quote_composition,
    replace_order_pricing_lines,
    serialize_composition_quote,
)
from .designations import create_designations, validate_designated_players
from .models import DesignatedOrderDraft, Order, OrderItem, OrderStatusLog
from .services import generate_order_no


def _draft_for_user(draft_id, user, *, for_update=False):
    queryset = DesignatedOrderDraft.objects.select_related(
        'package',
        'base_spec__package__package_family',
        'base_spec__required_player_type',
        'submitted_order',
    )
    if for_update:
        queryset = queryset.select_for_update()
    draft = queryset.filter(pk=draft_id).first()
    if not draft:
        raise ValidationError({'draft_id': '指定陪玩草稿不存在'})
    if draft.boss_user_id != getattr(user, 'id', None):
        raise PermissionDenied('无权操作该指定陪玩草稿')
    return draft


def _resolve_base_spec(base_spec_id):
    try:
        base_spec_id = int(base_spec_id)
    except (TypeError, ValueError) as exc:
        raise ValidationError({'base_spec_id': '基础规格参数不正确'}) from exc
    base_spec = (
        PackageSpec.objects.select_related('package__package_family', 'required_player_type')
        .filter(pk=base_spec_id)
        .first()
    )
    if not base_spec:
        raise ValidationError({'base_spec_id': '基础规格不存在'})
    return base_spec


def _duration(value):
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError({'booked_hours': '时长必须是正整数小时'}) from exc
    if value < 1 or value > 24:
        raise ValidationError({'booked_hours': '时长必须在 1 到 24 小时之间'})
    return value


def serialize_draft(draft, quote=None):
    data = {
        'id': draft.id,
        'status': draft.status,
        'boss_wechat': draft.boss_wechat,
        'game_id': draft.game_id,
        'package_id': draft.package_id,
        'base_spec_id': draft.base_spec_id,
        'designated_player_ids': draft.designated_player_ids or [],
        'booked_hours': draft.booked_hours,
        'boss_note': draft.boss_note,
        'submitted_order_no': draft.submitted_order.order_no if draft.submitted_order_id else None,
        'created_at': draft.created_at,
        'updated_at': draft.updated_at,
    }
    if quote is not None:
        data['quote'] = serialize_composition_quote(quote)
    return data


@transaction.atomic
def create_draft(*, user, payload):
    base_spec = _resolve_base_spec(payload.get('base_spec_id'))
    draft = DesignatedOrderDraft.objects.create(
        boss_user=user,
        boss_wechat=(payload.get('boss_wechat') or '').strip(),
        game_id=(payload.get('game_id') or '').strip() or None,
        package=base_spec.package,
        base_spec=base_spec,
        designated_player_ids=normalize_designated_player_ids(payload.get('designated_player_ids')),
        booked_hours=_duration(payload.get('booked_hours', 1)),
        boss_note=(payload.get('boss_note') or '').strip(),
    )
    return draft


@transaction.atomic
def update_draft(*, user, draft_id, payload):
    draft = _draft_for_user(draft_id, user, for_update=True)
    if draft.status != DesignatedOrderDraft.STATUS_DRAFT:
        raise ValidationError({'detail': '该草稿已经提交，不能继续修改'})

    if 'base_spec_id' in payload and payload.get('base_spec_id') is not None:
        base_spec = _resolve_base_spec(payload['base_spec_id'])
        draft.package = base_spec.package
        draft.base_spec = base_spec
    if 'designated_player_ids' in payload:
        draft.designated_player_ids = normalize_designated_player_ids(payload['designated_player_ids'])
    if 'booked_hours' in payload and payload.get('booked_hours') is not None:
        draft.booked_hours = _duration(payload['booked_hours'])
    if 'boss_wechat' in payload and payload.get('boss_wechat') is not None:
        draft.boss_wechat = str(payload['boss_wechat']).strip()
    if 'game_id' in payload:
        draft.game_id = (payload.get('game_id') or '').strip() or None
    if 'boss_note' in payload:
        draft.boss_note = (payload.get('boss_note') or '').strip()
    draft.save()
    return draft


def quote_draft(*, user, draft_id, require_virtual_binding=False):
    draft = _draft_for_user(draft_id, user)
    if draft.status != DesignatedOrderDraft.STATUS_DRAFT:
        raise ValidationError({'detail': '该草稿已经提交，不能再次报价'})
    quote = quote_composition(
        base_spec=draft.base_spec,
        designated_player_ids=draft.designated_player_ids or [],
        booked_hours=draft.booked_hours,
        require_virtual_binding=require_virtual_binding,
    )
    return draft, quote


@transaction.atomic
def submit_draft(*, user, draft_id):
    draft = _draft_for_user(draft_id, user, for_update=True)
    if draft.status == DesignatedOrderDraft.STATUS_SUBMITTED:
        if draft.submitted_order_id:
            return draft.submitted_order, False
        raise ValidationError({'detail': '草稿提交状态异常，请联系管理员'})

    if not (draft.boss_wechat or '').strip():
        raise ValidationError({'boss_wechat': '提交订单前请填写老板微信号'})
    if not (draft.game_id or '').strip():
        raise ValidationError({'game_id': '提交订单前请选择游戏'})

    # Recompute while the draft is locked.  Prices, player offers and the
    # static virtual-product binding are all server-side facts at submit time.
    quote = quote_composition(
        base_spec=draft.base_spec,
        designated_player_ids=draft.designated_player_ids or [],
        booked_hours=draft.booked_hours,
        require_virtual_binding=True,
    )

    # Reuse the established busy-player check immediately before invitations;
    # `quote_composition` owns type/offer/price validation.
    players = validate_designated_players(
        quote['designated_player_ids'],
        quote['required_players'],
    )
    if [player.id for player in players] != quote['designated_player_ids']:
        raise ValidationError({'designated_player_ids': '指定陪玩状态已变化，请重新报价'})

    virtual_spec = quote['virtual_package_spec']
    order = Order(
        order_no=generate_order_no(),
        boss_user=user,
        boss_wechat=draft.boss_wechat,
        game_id=draft.game_id,
        package=quote['base_package'],
        spec_id=quote['base_spec'].id,
        package_name_snapshot=f'{quote["package_family"].name} 指定陪玩组合',
        spec_name_snapshot=quote['base_spec'].display_name or quote['base_spec'].name,
        spec_price_snapshot=float(quote['base_spec'].price),
        required_players=quote['required_players'],
        designated_players=quote['designated_player_ids'] or None,
        boss_note=draft.boss_note or None,
        status=Order.STATUS_WAITING,
        is_custom=False,
        booked_hours=quote['booked_hours'],
        order_type=Order.ORDER_TYPE_NORMAL,
    )
    apply_quote_to_order(order, quote)
    order.save()

    # Keep exactly one order item: the internal static virtual SKU.  This is
    # intentionally not a normal-mall item; the display name makes that clear
    # while its price/quantity still reconcile with virtual payment.
    OrderItem.objects.create(
        order=order,
        package=virtual_spec.package,
        spec=virtual_spec,
        package_name=f'{quote["package_family"].name} 指定陪玩组合',
        spec_name=virtual_spec.name,
        spec_display_name='静态组合结算',
        unit_price=quote['total_price_per_hour'],
        quantity=quote['booked_hours'],
        amount=quote['total_amount'],
        image_url=quote['base_package'].cover_url or quote['base_package'].image_url or '',
        description='指定陪玩静态组合结算商品',
        sort_order=0,
    )
    replace_order_pricing_lines(order, quote)
    create_designations(order, players)

    OrderStatusLog.objects.create(
        order=order,
        to_status=order.status,
        operator=user,
        reason='创建静态组合指定陪玩订单；指定名额已发送邀请，其余名额进入公开抢单大厅',
    )
    draft.status = DesignatedOrderDraft.STATUS_SUBMITTED
    draft.submitted_order = order
    draft.save(update_fields=['status', 'submitted_order', 'updated_at'])
    return order, True
