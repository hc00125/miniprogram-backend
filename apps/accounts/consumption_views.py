from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.wallet.diamonds import DIAMONDS_PER_YUAN, yuan_to_diamonds

from .models import BossConsumptionLedger


def _positive_int(value, default, *, maximum=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    if maximum is not None:
        parsed = min(parsed, maximum)
    return parsed


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def consumption_records(request):
    profile = getattr(request.user, 'client_profile', None)
    if not profile:
        return Response({'detail': '请先微信登录'}, status=status.HTTP_404_NOT_FOUND)

    page = _positive_int(request.query_params.get('page'), 1)
    page_size = _positive_int(request.query_params.get('page_size'), 20, maximum=100)

    queryset = (
        BossConsumptionLedger.objects
        .filter(profile=profile)
        .select_related('order')
        .order_by('-created_at', '-id')
    )
    count = queryset.count()
    start = (page - 1) * page_size
    rows = queryset[start:start + page_size]

    results = []
    for row in rows:
        results.append({
            'id': row.id,
            'source_type': row.source_type,
            'source_type_text': row.get_source_type_display(),
            'amount_yuan': str(row.amount),
            'amount_diamonds': yuan_to_diamonds(row.amount),
            'balance_after_yuan': str(row.balance_after),
            'balance_after_diamonds': yuan_to_diamonds(row.balance_after),
            'order_no': row.order.order_no if row.order_id and row.order else '',
            'reference_id': row.reference_id,
            'reason': row.reason,
            'created_at': row.created_at,
        })

    return Response({
        'count': count,
        'page': page,
        'page_size': page_size,
        'diamonds_per_yuan': DIAMONDS_PER_YUAN,
        'growth_diamonds': yuan_to_diamonds(profile.cumulative_consumption),
        'results': results,
    })
