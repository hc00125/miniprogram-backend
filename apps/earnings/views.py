from django.db.models import Sum
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied

from apps.players.models import Player

from .models import PlayerEarning, Withdrawal
from .serializers import PlayerEarningSerializer, WithdrawalCreateSerializer, WithdrawalSerializer
from .services import create_withdrawal, ensure_wallet, get_earnings_config, release_due_earnings


def get_request_player(request):
    player = getattr(request.user, 'legacy_player', None)
    if player is None:
        try:
            player = request.user.player_profile
        except (AttributeError, Player.DoesNotExist):
            player = None
    if not player or player.status != Player.STATUS_APPROVED:
        raise PermissionDenied('当前账号不是已通过审核的陪玩师')
    return player


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def overview(request):
    player = get_request_player(request)
    release_due_earnings(player=player)
    wallet = ensure_wallet(player)
    config = get_earnings_config()
    earnings = PlayerEarning.objects.filter(player=player)
    totals = earnings.aggregate(
        gross_total=Sum('gross_amount'),
        commission_total=Sum('commission_amount'),
        net_total=Sum('net_amount'),
    )
    return Response({
        'player_id': player.id,
        'player_name': player.name,
        'completed_orders': earnings.count(),
        'accepted_orders': player.total_orders or 0,
        'default_commission_rate': config.default_commission_rate,
        'review_days': config.review_days,
        'min_withdrawal_amount': config.min_withdrawal_amount,
        'pending_balance': wallet.pending_balance,
        'available_balance': wallet.available_balance,
        'withdrawing_balance': wallet.withdrawing_balance,
        'withdrawn_total': wallet.withdrawn_total,
        'gross_total': totals['gross_total'] or 0,
        'commission_total': totals['commission_total'] or 0,
        'net_total': totals['net_total'] or 0,
        'can_withdraw': player.can_withdraw,
        'withdrawal_block_reason': '' if player.can_withdraw else '管理员已暂停您的提现权限，请联系客服处理',
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def settlements(request):
    player = get_request_player(request)
    release_due_earnings(player=player)
    queryset = (
        PlayerEarning.objects
        .filter(player=player)
        .select_related('order__package')
        .order_by('-created_at')[:100]
    )
    return Response({
        'count': PlayerEarning.objects.filter(player=player).count(),
        'results': PlayerEarningSerializer(queryset, many=True).data,
    })


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def withdrawals(request):
    player = get_request_player(request)
    if request.method == 'GET':
        queryset = Withdrawal.objects.filter(player=player).order_by('-created_at')[:100]
        return Response({
            'count': Withdrawal.objects.filter(player=player).count(),
            'results': WithdrawalSerializer(queryset, many=True).data,
        })

    if not player.can_withdraw:
        return Response(
            {'detail': '管理员已暂停您的提现权限，请联系客服处理'},
            status=status.HTTP_403_FORBIDDEN,
        )
    serializer = WithdrawalCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    withdrawal = create_withdrawal(player=player, **serializer.validated_data)
    return Response(
        WithdrawalSerializer(withdrawal).data,
        status=status.HTTP_201_CREATED,
    )
