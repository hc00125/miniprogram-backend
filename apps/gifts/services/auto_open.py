"""Event-scoped, audited, missing-only ordinary gift configuration."""
from apps.common.gift_opening import gift_opening_write
from apps.accounts.models import ClientProfile
from apps.players.models import Player
from apps.gifts.models import Gift
from .configuration import _create_missing_config

SYSTEM_REASON = 'system:gift-auto-open:v1 自动开放普通上架礼物；仅补缺，保留人工停用与既有比例'


def eligible_players():
    from apps.common.player_model_version import archive_filter
    return Player.objects.filter(
        **archive_filter(Player), status=Player.STATUS_APPROVED, is_publicly_visible=True,
        user__is_active=True,
        user_id__in=ClientProfile.objects.filter(account_status=ClientProfile.ACCOUNT_STATUS_ACTIVE).values('user_id'),
    )


@gift_opening_write()
def open_missing(*, gift_id=None, player_id=None):
    if gift_id is None and player_id is None:
        raise ValueError('An explicit event target is required; no global scan')
    gifts = Gift.objects.filter(kind=Gift.KIND_GIFT, is_active=True)
    players = eligible_players()
    if gift_id is not None:
        gifts = gifts.filter(pk=gift_id)
    if player_id is not None:
        players = players.filter(pk=player_id)
    count = 0
    gift_ids = list(gifts.order_by('pk').values_list('pk', flat=True))
    for pk in players.order_by('pk').values_list('pk', flat=True):
        for gid in gift_ids:
            count += _create_missing_config(player_id=pk, gift_id=gid, actor=None, reason=SYSTEM_REASON)
    return count
