"""Archive identity only; never settle, cancel, delete or rewrite money/history."""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from .models import Player, PlayerArchiveEvent, PlayerServiceListing


def _transition(ids, *, actor, reason, archived):
    reason = (reason or '').strip()
    if not reason or not actor or not actor.pk:
        raise ValidationError('必须填写原因并记录操作者')
    with transaction.atomic():
        players = list(Player.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
        if {p.pk for p in players} != set(ids):
            raise ValidationError('陪玩ID已变化，请重新预览')
        for player in players:
            if player.is_archived == archived:
                continue
            values = dict(is_archived=archived, is_online=False, presence_seen_at=None,
                          can_accept_orders=False, can_be_designated=False, is_publicly_visible=False)
            if archived:
                # This retained timestamp also fences stale model saves. Make
                # each archive epoch distinct even if the clock repeats/backtracks.
                archived_at = timezone.now()
                if player.archived_at is not None:
                    archived_at = max(archived_at, player.archived_at + timedelta(microseconds=1))
                values.update(archived_at=archived_at, archived_by=actor,
                              archive_reason=reason, archived_name=player.name)
            Player.objects.filter(pk=player.pk).update(**values)
            PlayerServiceListing.objects.filter(player=player).update(is_available=False)
            PlayerArchiveEvent.objects.create(player=player, actor=actor,
                action='archive' if archived else 'restore', reason=reason, player_name=player.name)
        return players


def ensure_new_business(player_ids):
    """Serialize new commitments with archive; call inside the write transaction.

    Quotes may use this outside a transaction for a fresh read. No lock on user,
    wallet or orders is acquired here, so archive itself only locks player rows.
    """
    from django.db import connection
    from rest_framework.exceptions import ValidationError as APIValidationError
    ids = sorted({int(pk) for pk in player_ids if pk})
    qs = Player.objects.filter(pk__in=ids).order_by('pk')
    if connection.in_atomic_block:
        qs = qs.select_for_update()
    players = list(qs)
    if len(players) != len(ids) or any(p.is_archived for p in players):
        raise APIValidationError({'code': 'PLAYER_ARCHIVED', 'detail': '该陪玩已离开，不能接收新业务'})
    return players


def archive_players(ids, *, actor, reason):
    return _transition(ids, actor=actor, reason=reason, archived=True)


def restore_players(ids, *, actor, reason):
    return _transition(ids, actor=actor, reason=reason, archived=False)
