from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db.models import Count, DecimalField, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import render
from django.utils import timezone

from apps.earnings.models import PlayerEarning, WalletLedger, Withdrawal
from apps.payments.models import Payment, Refund
from apps.wallet.models import ClientWalletLedger, RechargeOrder


ZERO = Decimal('0.00')
CENT = Decimal('0.01')
MONEY_FIELD = DecimalField(max_digits=18, decimal_places=2)
PAID_PAYMENT_STATUSES = ('paid', 'refunded')
SHANGHAI_TZ = ZoneInfo('Asia/Shanghai')


def _money(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def _sum(queryset, field):
    return _money(
        queryset.aggregate(total=Coalesce(Sum(field), ZERO, output_field=MONEY_FIELD))['total']
    )


def _sum_items(items, field):
    return _money(sum((Decimal(str(getattr(item, field) or 0)) for item in items), ZERO))


def _parse_event_time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, SHANGHAI_TZ)
    return parsed


def _refund_business_time(refund):
    payload = refund.notify_payload if isinstance(refund.notify_payload, dict) else {}
    balance_meta = payload.get('balance_refund') if isinstance(payload.get('balance_refund'), dict) else {}
    cash_meta = payload.get('wechat_original_refund') if isinstance(payload.get('wechat_original_refund'), dict) else {}
    return (
        _parse_event_time(balance_meta.get('settled_at'))
        or _parse_event_time(cash_meta.get('succeeded_at'))
        or refund.updated_at
    )


def _refund_cash_time(refund):
    payload = refund.notify_payload if isinstance(refund.notify_payload, dict) else {}
    cash_meta = payload.get('wechat_original_refund') if isinstance(payload.get('wechat_original_refund'), dict) else {}
    is_cash_success = (
        cash_meta.get('status') == 'succeeded'
        or payload.get('wechat_coin_refund_status') == 'succeeded'
    )
    if not is_cash_success:
        return None
    return (
        _parse_event_time(cash_meta.get('succeeded_at'))
        or _parse_event_time(payload.get('wechat_coin_refund_succeeded_at'))
        or refund.updated_at
    )


def _date_bounds(day, end_day=None):
    end_day = end_day or day
    if day > end_day:
        raise ValueError('开始日期不能晚于结束日期')
    if end_day == date.max:
        raise ValueError('结束日期必须早于 9999-12-31')
    start = datetime.combine(day, time.min, tzinfo=SHANGHAI_TZ)
    end = datetime.combine(end_day or day, time.min, tzinfo=SHANGHAI_TZ) + timedelta(days=1)
    return start, end


def _yuan_from_fish(value):
    rate = Decimal(str(settings.FISH_CRACKER_EXCHANGE_RATE))
    if rate <= ZERO:
        return ZERO
    return _money(_money(value) / rate)


def build_daily_billing_report(day, end_day=None):
    """Read-only report for inclusive Shanghai dates; one argument keeps single-day behavior."""
    start, end = _date_bounds(day, end_day)

    payments = (
        Payment.objects.filter(
            status__in=PAID_PAYMENT_STATUSES,
            paid_at__gte=start,
            paid_at__lt=end,
        )
        .exclude(qr_code__startswith='mockpay://')
        .select_related('order', 'order__boss_user')
        .order_by('-paid_at', '-id')
    )
    successful_refunds = list(
        Refund.objects.filter(status=Refund.STATUS_SUCCEEDED)
        .select_related('payment', 'order', 'order__boss_user')
    )
    refunds = []
    cash_refunds = []
    for refund in successful_refunds:
        business_at = _refund_business_time(refund)
        if start <= business_at < end:
            refund.billing_event_at = business_at
            refunds.append(refund)
        cash_at = _refund_cash_time(refund)
        if cash_at is not None and start <= cash_at < end:
            refund.billing_cash_event_at = cash_at
            cash_refunds.append(refund)
    refunds.sort(key=lambda item: (item.billing_event_at, item.id), reverse=True)
    cash_refunds.sort(key=lambda item: (item.billing_cash_event_at, item.id), reverse=True)
    recharges = (
        RechargeOrder.objects.filter(
            status=RechargeOrder.STATUS_CREDITED,
            paid_at__gte=start,
            paid_at__lt=end,
        )
        .exclude(channel=RechargeOrder.CHANNEL_MOCK)
        .select_related('profile')
        .order_by('-paid_at', '-id')
    )
    standalone_recharges = recharges.filter(checkout_order_no='')
    linked_recharges = recharges.exclude(checkout_order_no='')
    direct_cash_payments = payments.filter(channel__in=('wechat', 'wechat_virtual'))

    earnings = (
        PlayerEarning.objects.filter(created_at__gte=start, created_at__lt=end)
        .select_related('order', 'player')
        .order_by('-created_at', '-id')
    )
    paid_withdrawals = (
        Withdrawal.objects.filter(
            status=Withdrawal.STATUS_PAID,
            paid_at__gte=start,
            paid_at__lt=end,
        )
        .select_related('player')
        .order_by('-paid_at', '-id')
    )
    boss_ledgers = (
        ClientWalletLedger.objects.filter(created_at__gte=start, created_at__lt=end)
        .select_related('wallet__profile', 'operator')
        .order_by('-created_at', '-id')
    )
    player_ledgers = (
        WalletLedger.objects.filter(created_at__gte=start, created_at__lt=end)
        .select_related('wallet__player', 'operator')
        .order_by('-created_at', '-id')
    )

    recharge_received = _sum(standalone_recharges, 'amount')
    linked_checkout_received = _sum(linked_recharges, 'amount')
    direct_order_received = _sum(direct_cash_payments, 'amount')
    cash_received = _money(recharge_received + linked_checkout_received + direct_order_received)
    order_revenue = _sum(payments, 'amount')
    refund_amount = _sum_items(refunds, 'amount')
    cash_refund_amount = _sum_items(cash_refunds, 'amount')
    commission_fish = _sum(earnings, 'commission_amount')
    wages_fish = _sum(earnings, 'net_amount')
    withdrawal_fish = _sum(paid_withdrawals, 'amount')
    withdrawal_yuan = _yuan_from_fish(withdrawal_fish)

    summary = {
        'recharge_received': recharge_received,
        'linked_checkout_received': linked_checkout_received,
        'direct_order_received': direct_order_received,
        'cash_received': cash_received,
        'cash_receipt_count': recharges.count() + direct_cash_payments.count(),
        'order_revenue': order_revenue,
        'refund_amount': refund_amount,
        'cash_refund_amount': cash_refund_amount,
        'cash_refund_count': len(cash_refunds),
        'platform_commission_fish': commission_fish,
        'platform_commission_yuan': _yuan_from_fish(commission_fish),
        'player_wages_fish': wages_fish,
        'player_wages_yuan': _yuan_from_fish(wages_fish),
        'withdrawal_fish': withdrawal_fish,
        'withdrawal_yuan': withdrawal_yuan,
        'net_cash_flow': _money(cash_received - cash_refund_amount - withdrawal_yuan),
        'order_count': payments.aggregate(total=Count('order_id', distinct=True))['total'] or 0,
        'payment_count': payments.count(),
        'refund_count': len(refunds),
        'recharge_count': standalone_recharges.count(),
        'withdrawal_count': paid_withdrawals.count(),
        'earning_count': earnings.count(),
    }

    return {
        'day': day,
        'start': start,
        'end': end,
        'summary': summary,
        'payments': payments,
        'refunds': refunds,
        'recharges': recharges,
        'earnings': earnings,
        'withdrawals': paid_withdrawals,
        'boss_ledgers': boss_ledgers,
        'player_ledgers': player_ledgers,
    }


def _parse_day(raw):
    try:
        parsed = date.fromisoformat(raw)
        if parsed.isoformat() == raw:
            return parsed
    except (TypeError, ValueError):
        pass
    raise ValueError('请输入有效日期，格式为 YYYY-MM-DD')


def _parse_range(params):
    keys = ('date', 'start_date', 'end_date', 'preset')
    if any(len(params.getlist(key)) > 1 for key in keys):
        raise ValueError('日期参数不能重复')
    has_range = 'start_date' in params or 'end_date' in params
    if sum(('date' in params, has_range, 'preset' in params)) > 1:
        raise ValueError('单日、日期范围和快捷日期只能选择一种')
    if 'preset' in params:
        today = timezone.now().astimezone(SHANGHAI_TZ).date()
        preset = params['preset']
        if preset == 'today':
            return today, today
        if preset == 'this_week':
            monday = today - timedelta(days=today.weekday())
            return monday, monday + timedelta(days=6)
        if preset == 'this_month':
            first = today.replace(day=1)
            next_month = (first + timedelta(days=32)).replace(day=1)
            return first, next_month - timedelta(days=1)
        if preset == 'last_month':
            last = today.replace(day=1) - timedelta(days=1)
            return last.replace(day=1), last
        raise ValueError('无效的快捷日期')
    if has_range:
        return _parse_day(params.get('start_date')), _parse_day(params.get('end_date'))
    day = _parse_day(params['date']) if 'date' in params else timezone.now().astimezone(SHANGHAI_TZ).date()
    return day, day


def daily_billing_view(request):
    required_permissions = ('wallet.view_clientwalletledger', 'earnings.view_walletledger')
    if not request.user.is_superuser and not request.user.has_perms(required_permissions):
        raise PermissionDenied('没有查看每日账单的权限')

    try:
        day, end_day = _parse_range(request.GET)
        _date_bounds(day, end_day)
    except ValueError as exc:
        return render(request, 'admin/daily_billing.html', {
            **admin.site.each_context(request),
            'title': '每日账单',
            'filter_error': str(exc),
            'selected_date': request.GET.get('date', ''),
            'selected_start_date': request.GET.get('start_date', ''),
            'selected_end_date': request.GET.get('end_date', ''),
        }, status=400)
    report = build_daily_billing_report(day, end_day)
    context = {
        **admin.site.each_context(request),
        **report,
        'title': '每日账单',
        'selected_date': day.isoformat(),
        'selected_start_date': day.isoformat(),
        'selected_end_date': end_day.isoformat(),
        'is_single_day': day == end_day,
        'previous_date': (day - timedelta(days=1)).isoformat() if day > date.min else '',
        'next_date': (day + timedelta(days=1)).isoformat() if day < date.max - timedelta(days=1) else '',
        'today': timezone.now().astimezone(SHANGHAI_TZ).date().isoformat(),
        'fish_rate': settings.FISH_CRACKER_EXCHANGE_RATE,
    }
    return render(request, 'admin/daily_billing.html', context)
