"""Read-only public rankings. Renewals are duration purchases, not service completions."""
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Count
from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import Order, OrderPlayer


def public_avatar(value):
    value = value or ''
    if any(c.isspace() for c in value) or '\\' in value:
        return ''
    return value if (value.startswith('https://') or
                     (value.startswith('/') and not value.startswith('//'))) else ''


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def rankings(request):
    board = request.query_params.get('board', 'boss')
    period = request.query_params.get('period', 'day')
    if board not in {'boss', 'player'} or period not in {'day', 'week', 'month'}:
        return Response({'detail': '榜单或周期不正确'}, status=400)
    if not getattr(settings, 'RANKINGS_ENABLED', False):
        return Response({'available': False})
    now = timezone.now().astimezone(ZoneInfo('Asia/Shanghai'))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'week':
        start -= timedelta(days=start.weekday())
        end = start + timedelta(days=7)
    elif period == 'month':
        start = start.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        end = start + timedelta(days=1)
    if board == 'boss':
        from .ranking_contributions import boss_results
        return Response({'available': True, 'board': board, 'period': period,
                         'period_start': start.isoformat(), 'period_end': end.isoformat(),
                         'updated_at': now.isoformat(), 'results': boss_results(start, end)})
    rows = (OrderPlayer.objects.filter(
        order__status=Order.STATUS_COMPLETED, order__order_type=Order.ORDER_TYPE_NORMAL, status='已完成',
        grab_time__gte=start, grab_time__lt=end,
        player__is_archived=False, player__is_publicly_visible=True,
    ).values('player_id', 'player__name', 'player__user__client_profile__avatar_url')
      .annotate(value=Count('order_id', distinct=True)).order_by('-value', 'player_id')[:50])
    results = []
    last_value, rank = None, 0
    for index, row in enumerate(rows, 1):
        if row['value'] != last_value:
            rank = index
        last_value = row['value']
        results.append({'id': str(row['player_id']), 'rank': rank,
                        'name': row['player__name'].strip() or '陪玩',
                        'avatar_url': public_avatar(row['player__user__client_profile__avatar_url']),
                        'value': row['value']})
    return Response({'available': True, 'board': board, 'period': period,
                     'period_start': start.isoformat(), 'period_end': end.isoformat(),
                     'updated_at': now.isoformat(), 'results': results})
