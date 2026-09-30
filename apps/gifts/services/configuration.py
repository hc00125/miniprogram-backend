"""Explicit player×gift rates only; no hierarchy or runtime fallback."""
from decimal import Decimal
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from apps.players.models import Player
from apps.gifts.models import PlayerGiftConfig, PlayerGiftConfigAudit


def require_operator(actor, permission, reason):
    if not actor or not actor.is_active or not actor.is_staff or not actor.has_perm(permission):
        raise PermissionDenied('需要平台管理员专项权限')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 300:
        raise ValidationError('必须填写不超过300字的操作原因')


def snapshot(config):
    return {'config_id': config.pk, 'player_id': config.player_id, 'gift_id': config.gift_id,
            'commission_rate': format(config.commission_rate, '.2f'),
            'version': config.version, 'is_enabled': config.is_enabled}


@transaction.atomic
def initialize_configs(*, actor, players, gifts, reason):
    require_operator(actor, 'gifts.add_playergiftconfig', reason)
    gift_ids = sorted({gift.pk for gift in gifts})
    created = 0
    # Parent locks also serialize initialization when no association row exists yet.
    for player in Player.objects.select_for_update().filter(pk__in=[p.pk for p in players]).order_by('pk'):
        for gift_id in gift_ids:
            if PlayerGiftConfig.objects.filter(player=player, gift_id=gift_id).exists():
                continue
            config = PlayerGiftConfig(player=player, gift_id=gift_id)
            config.save(_audited=True)
            PlayerGiftConfigAudit.objects.create(config=config, actor=actor, reason=reason.strip(), after=snapshot(config))
            created += 1
    return created


@transaction.atomic
def update_config(*, actor, config_id, commission_rate, is_enabled, reason, expected_version):
    require_operator(actor, 'gifts.change_playergiftconfig', reason)
    config = PlayerGiftConfig.objects.select_for_update().get(pk=config_id)
    if config.version != expected_version:
        raise ValidationError('CONFIG_CHANGED：配置已变更，请刷新后确认')
    before = snapshot(config)
    config.commission_rate = commission_rate
    config.is_enabled = is_enabled
    config.version += 1
    config.save(_audited=True)
    PlayerGiftConfigAudit.objects.create(config=config, actor=actor, reason=reason.strip(), before=before, after=snapshot(config))
    return config


def resolve_commission(player_id, gift_id, *, expected_version=None):
    try:
        config = PlayerGiftConfig.objects.get(player_id=player_id, gift_id=gift_id, is_enabled=True)
    except PlayerGiftConfig.DoesNotExist as exc:
        raise ValidationError('GIFT_CONFIG_UNAVAILABLE：未配置或已停用，不允许赠送') from exc
    config.full_clean()
    if expected_version is not None and config.version != expected_version:
        raise ValidationError('CONFIG_CHANGED：配置已变更，请重新确认')
    return snapshot(config)
