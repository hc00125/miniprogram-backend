"""Public read-only projections; intentionally bypass all DRF authenticators.

Only the explicit DTO fields below leave this endpoint. Never call payment,
fulfillment, income or profile-repair services while rendering announcements.
"""
from django.db.models import Exists, F, OuterRef, Prefetch, Q, Subquery, Sum, Value
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.gifts.models import GiftTransfer, GiftTransferAllocation
from .models import CrownGrant, PatronagePurchase


def gift_rows():
    allocated = GiftTransferAllocation.objects.filter(transfer_id=OuterRef('pk')).order_by().values('transfer_id').annotate(
        total=Sum('quantity')).values('total')
    return GiftTransfer.objects.filter(status='delivered', gift__kind='gift', created_at__lte=timezone.now()).alias(
        allocated_quantity=Subquery(allocated),
    ).filter(
        Q(source='inventory', allocated_quantity=F('quantity'))
        | Q(source='direct', purchase__status='paid', purchase__attempt__status='completed',
            purchase__quantity=F('quantity'), purchase__buyer_id=F('sender_id'),
            purchase__recipient_id=F('recipient_id'), purchase__gift_id=F('gift_id'))
    ).select_related('sender__client_profile', 'recipient__user__client_profile', 'gift', 'purchase').prefetch_related(
        Prefetch('allocations', queryset=GiftTransferAllocation.objects.only('transfer_id', 'snapshot').order_by('pk'))
    ).order_by('-created_at', '-pk')


def public_avatar(user):
    profile = getattr(user, 'client_profile', None)
    return (profile.avatar_url or '') if profile else ''


def gift_data(row, request):
    profile = getattr(row.sender, 'client_profile', None)
    boss = (profile.nickname.strip() if profile else '') or '老板'
    snapshots = [row.purchase.snapshot] if row.purchase_id else [a.snapshot for a in row.allocations.all()]
    name = next((s['name'].strip() for s in snapshots
                 if isinstance(s, dict) and isinstance(s.get('name'), str) and s['name'].strip()), row.gift.name)
    return {
        'id': f'gift:{row.pk}', 'kind': 'gift', 'boss_name': boss,
        'boss_avatar_url': public_avatar(row.sender),
        'player_avatar_url': public_avatar(row.recipient.user),
        'item_image_url': request.build_absolute_uri(row.gift.image.url) if row.gift.image else '',
        'player_id': row.recipient_id, 'player_name': row.recipient.name,
        'item_name': name, 'quantity': row.quantity, 'occurred_at': row.created_at.isoformat(),
        'text': f'老板「{boss}」赠送「{row.recipient.name}」{row.quantity}个「{name}」',
    }


def patronage_rows():
    grants = CrownGrant.objects.filter(purchase_id=OuterRef('pk'), revoked_at__isnull=True,
        starts_at=OuterRef('starts_at'), expires_at=OuterRef('expires_at'), expires_at__gt=F('starts_at')).filter(
            Q(purchase__package_code='day_pass', source='day_pass_bonus')
            | (~Q(purchase__package_code='day_pass') & Q(source='purchase')))
    return PatronagePurchase.objects.filter(
        payment_status='paid', attempt__status='completed', paid_at__lte=timezone.now(),
        earning__isnull=False, starts_at=F('paid_at'), expires_at__gt=F('starts_at'),
    ).filter(Exists(grants)).annotate(is_renewal=Exists(grants.filter(
        extension_starts_at__gt=F('starts_at'), duration_days__gt=0,
        expires_at__gt=F('extension_starts_at'),
    ))).select_related('boss__client_profile', 'player__user__client_profile').order_by('-paid_at', '-pk')


def patronage_data(row):
    profile = getattr(row.boss, 'client_profile', None)
    boss = (profile.nickname.strip() if profile else '') or '老板'
    verb = '续购' if row.is_renewal else '开通'
    text = (f'老板「{boss}」包天「{row.player_name}」，赠送7天冠名'
            if row.package_code == 'day_pass'
            else f'老板「{boss}」为「{row.player_name}」{verb}「{row.package_name}」')
    return {
        'id': f'patronage:{row.pk}', 'kind': 'patronage', 'boss_name': boss,
        'boss_avatar_url': public_avatar(row.boss),
        'player_avatar_url': public_avatar(row.player.user),
        'item_image_url': '',
        'player_id': row.player_id, 'player_name': row.player_name,
        'item_name': row.package_name, 'quantity': 1, 'occurred_at': row.paid_at.isoformat(),
        'text': text,
    }


def all_data(request, start, page_size):
    gifts, patronages = gift_rows(), patronage_rows()
    # Page only scalar event keys in PostgreSQL, not each source independently.
    # kind + numeric PK are a total tie-breaker even when both tables share IDs.
    events = gifts.order_by().values('pk', event_at=F('created_at'), event_kind=Value('gift')).union(
        patronages.order_by().values('pk', event_at=F('paid_at'), event_kind=Value('patronage')),
        all=True,
    ).order_by('-event_at', '-event_kind', '-pk')
    count = events.count()
    if start >= count:
        return count, []
    keys = list(events[start:start + page_size])
    gift_ids = [event['pk'] for event in keys if event['event_kind'] == 'gift']
    patronage_ids = [event['pk'] for event in keys if event['event_kind'] == 'patronage']
    data = {('gift', row.pk): gift_data(row, request) for row in gifts.filter(pk__in=gift_ids)}
    data.update({('patronage', row.pk): patronage_data(row) for row in patronages.filter(pk__in=patronage_ids)})
    return count, [data[(event['event_kind'], event['pk'])] for event in keys]


@require_GET
def announcements(request):
    kind = request.GET.get('kind', 'patronage')
    try:
        raw_page = request.GET.get('page', '1')
        raw_size = request.GET.get('page_size', '20')
        if not all(value.isascii() and value.isdecimal() for value in (raw_page, raw_size)):
            raise ValueError
        page, page_size = int(raw_page), int(raw_size)
        if kind not in ('patronage', 'gift', 'all') or page < 1 or not 1 <= page_size <= 50:
            raise ValueError
    except ValueError:
        return JsonResponse({'detail': '通报类型或分页参数无效'}, status=400)
    start = (page - 1) * page_size
    if kind == 'all':
        count, results = all_data(request, start, page_size)
    else:
        rows, serialize = ((patronage_rows(), patronage_data) if kind == 'patronage'
                           else (gift_rows(), lambda row: gift_data(row, request)))
        count = rows.count()
        results = [serialize(row) for row in rows[start:start + page_size]] if start < count else []
    return JsonResponse({'count': count, 'page': page, 'page_size': page_size, 'results': results})
