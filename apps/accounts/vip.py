from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import BossConsumptionLedger, ClientProfile, ClientVipKookRoom, VipTier


ZERO = Decimal('0.00')
CENT = Decimal('0.01')
PRIVATE_KOOK_ROOM_FEATURE = 'private_kook_room'
PRIVATE_KOOK_ROOM_UNLOCK_DIAMONDS = 20000


def qmoney(value):
    try:
        return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({'amount': '金额格式不正确'}) from exc


def resolve_vip_tier(total_consumption):
    total = max(ZERO, qmoney(total_consumption))
    return (
        VipTier.objects
        .filter(is_active=True, min_consumption__lte=total)
        .order_by('-min_consumption', '-sort_order', '-id')
        .first()
    )


def get_next_vip_tier(total_consumption):
    total = max(ZERO, qmoney(total_consumption))
    return (
        VipTier.objects
        .filter(is_active=True, min_consumption__gt=total)
        .order_by('min_consumption', 'sort_order', 'id')
        .first()
    )


def tier_has_feature(tier, feature_code):
    if not tier:
        return False
    return feature_code in (tier.feature_codes or [])


def private_kook_room_snapshot(profile, current_tier=None):
    current_tier = current_tier or resolve_vip_tier(profile.cumulative_consumption)
    unlocked = tier_has_feature(current_tier, PRIVATE_KOOK_ROOM_FEATURE)
    room = (
        ClientVipKookRoom.objects
        .select_related('assigned_by')
        .filter(profile=profile)
        .first()
    )
    room_number = str(getattr(room, 'kook_room_number', '') or '').strip()
    configured = bool(room_number)
    active = bool(room and room.is_active)
    available = bool(unlocked and configured and active)

    if not unlocked:
        status = 'locked'
    elif not configured:
        status = 'pending_configuration'
    elif not active:
        status = 'disabled'
    else:
        status = 'active'

    return {
        'feature_code': PRIVATE_KOOK_ROOM_FEATURE,
        'unlock_diamonds': PRIVATE_KOOK_ROOM_UNLOCK_DIAMONDS,
        'unlocked': unlocked,
        'configured': configured,
        'active': active,
        'available': available,
        'status': status,
        'room_number': room_number,
    }


def vip_snapshot(profile):
    total = max(ZERO, qmoney(profile.cumulative_consumption))
    current = resolve_vip_tier(total) or profile.vip_tier
    next_tier = get_next_vip_tier(total)

    current_floor = qmoney(current.min_consumption) if current else ZERO
    if next_tier:
        next_floor = qmoney(next_tier.min_consumption)
        span = max(CENT, next_floor - current_floor)
        progress = min(100, max(0, int(((total - current_floor) / span * 100).quantize(Decimal('1')))))
        remaining = max(ZERO, next_floor - total)
    else:
        progress = 100
        remaining = ZERO

    def tier_payload(tier):
        if not tier:
            return None
        return {
            'id': tier.id,
            'code': tier.code,
            'name': tier.name,
            'min_consumption': str(qmoney(tier.min_consumption)),
            'benefits': tier.benefits or [],
            'feature_codes': tier.feature_codes or [],
            'badge_color': tier.badge_color,
        }

    return {
        'cumulative_consumption': str(total),
        'current_tier': tier_payload(current),
        'next_tier': tier_payload(next_tier),
        'remaining_to_next': str(remaining),
        'progress_percent': progress,
        'private_kook_room': private_kook_room_snapshot(profile, current),
    }


def _operator_or_none(operator):
    return operator if getattr(operator, 'is_authenticated', False) else None


@transaction.atomic
def record_consumption(
    *,
    profile,
    amount,
    source_type,
    order=None,
    reference_id='',
    reason='',
    operator=None,
):
    delta = qmoney(amount)
    if delta == ZERO:
        raise ValidationError({'amount': '消费变动不能为0'})
    if source_type not in dict(BossConsumptionLedger.TYPE_CHOICES):
        raise ValidationError({'source_type': '消费流水来源不正确'})

    profile = ClientProfile.objects.select_for_update(of=('self',)).select_related('vip_tier').get(pk=profile.pk)
    reference_id = str(reference_id or '').strip()
    if reference_id:
        existing = BossConsumptionLedger.objects.filter(
            profile=profile,
            source_type=source_type,
            reference_id=reference_id,
        ).first()
        if existing:
            return existing

    old_total = qmoney(profile.cumulative_consumption)
    new_total = max(ZERO, qmoney(old_total + delta))
    applied_delta = qmoney(new_total - old_total)
    if applied_delta == ZERO:
        # 例如累计消费已经为0时再次收到退款回调，不再制造无意义流水。
        return None

    tier = resolve_vip_tier(new_total)
    profile.cumulative_consumption = new_total
    profile.vip_tier = tier
    profile.vip_updated_at = timezone.now()
    profile.save(update_fields=['cumulative_consumption', 'vip_tier', 'vip_updated_at', 'updated_at'])

    try:
        return BossConsumptionLedger.objects.create(
            profile=profile,
            amount=applied_delta,
            balance_after=new_total,
            source_type=source_type,
            order=order,
            reference_id=reference_id,
            reason=(reason or '')[:500],
            operator=_operator_or_none(operator),
        )
    except IntegrityError:
        if reference_id:
            return BossConsumptionLedger.objects.get(
                profile=profile,
                source_type=source_type,
                reference_id=reference_id,
            )
        raise


def resolve_order_profile(order):
    if getattr(order, 'boss_user_id', None):
        profile = getattr(order.boss_user, 'client_profile', None)
        if profile:
            return profile
    openid = str(getattr(order, 'boss_wechat', '') or '').strip()
    if openid:
        return ClientProfile.objects.filter(openid=openid).first()
    return None


def apply_vip_kook_room_to_order(order):
    if str(getattr(order, 'kook_room_number', '') or '').strip():
        return False

    profile = resolve_order_profile(order)
    if not profile:
        return False

    current_tier = resolve_vip_tier(profile.cumulative_consumption)
    snapshot = private_kook_room_snapshot(profile, current_tier)
    if not snapshot['available']:
        return False

    room = (
        ClientVipKookRoom.objects
        .select_related('assigned_by')
        .filter(profile=profile)
        .first()
    )
    if not room:
        return False

    order.kook_room_number = snapshot['room_number']
    order.kook_room_updated_at = timezone.now()
    order.kook_room_updated_by = room.assigned_by
    order.save(update_fields=[
        'kook_room_number',
        'kook_room_updated_at',
        'kook_room_updated_by',
    ])
    return True


def record_completed_order(order):
    from apps.orders.models import Order

    if not order.paid or order.status != Order.STATUS_COMPLETED:
        return None
    profile = resolve_order_profile(order)
    if not profile:
        return None
    amount = qmoney(order.total_amount if order.total_amount is not None else order.total_price_per_hour)
    if amount <= ZERO:
        return None
    return record_consumption(
        profile=profile,
        amount=amount,
        source_type=BossConsumptionLedger.TYPE_ORDER,
        order=order,
        reference_id=order.order_no,
        reason=f'订单 {order.order_no} 完成并计入累计消费',
    )


def record_successful_refund(refund, operator=None):
    from apps.payments.models import Refund

    if refund.status != Refund.STATUS_SUCCEEDED:
        return None
    profile = resolve_order_profile(refund.order)
    if not profile:
        return None
    amount = qmoney(refund.amount)
    if amount <= ZERO:
        return None
    return record_consumption(
        profile=profile,
        amount=-amount,
        source_type=BossConsumptionLedger.TYPE_REFUND,
        order=refund.order,
        reference_id=refund.refund_no,
        reason=f'退款 {refund.refund_no} 成功，扣减累计消费',
        operator=operator,
    )


def create_manual_consumption_adjustment(*, profile, amount, reason, order=None, operator=None):
    reason = str(reason or '').strip()
    if not reason:
        raise ValidationError({'reason': '人工调整必须填写原因'})
    return record_consumption(
        profile=profile,
        amount=amount,
        source_type=BossConsumptionLedger.TYPE_MANUAL,
        order=order,
        reason=reason,
        operator=operator,
    )
