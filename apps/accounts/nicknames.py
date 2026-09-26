import hashlib
import hmac

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.players.models import Player, PlayerApplication

from .models import ClientProfile


DEFAULT_NICKNAME_PREFIX = '微信用户-'
DEFAULT_NICKNAME_SUFFIX_LENGTHS = (10, 12, 16, 24, 32, 48, 64)


def default_nickname_digest(openid):
    """根据 OpenID 生成稳定但不直接暴露原始 OpenID 的摘要。"""
    key = str(settings.SECRET_KEY).encode('utf-8')
    message = str(openid or '').encode('utf-8')
    return hmac.new(key, message, hashlib.sha256).hexdigest().upper()


def default_nickname_candidates(openid):
    """按长度递增生成候选昵称；极小概率冲突时自动扩展摘要。"""
    digest = default_nickname_digest(openid)
    for length in DEFAULT_NICKNAME_SUFFIX_LENGTHS:
        yield f'{DEFAULT_NICKNAME_PREFIX}{digest[:length]}'


def nickname_is_available(nickname):
    """默认昵称不能占用客户、正式陪玩或有效陪玩申请中的名称。"""
    if ClientProfile.objects.filter(nickname__iexact=nickname).exists():
        return False
    if Player.objects.filter(name__iexact=nickname).exists():
        return False
    return not PlayerApplication.objects.filter(
        name__iexact=nickname,
        status__in=[
            PlayerApplication.STATUS_PENDING,
            PlayerApplication.STATUS_APPROVED,
        ],
    ).exists()


def make_default_nickname(openid):
    """返回当前可用的稳定默认昵称。"""
    if not openid:
        raise ValueError('缺少 OpenID，无法生成默认昵称')
    for nickname in default_nickname_candidates(openid):
        if nickname_is_available(nickname):
            return nickname
    raise RuntimeError('默认昵称生成失败，请重新登录')


def get_or_create_wechat_profile(user, openid):
    """创建微信客户资料，并在唯一约束竞争时自动尝试更长摘要。

    同一 OpenID 会映射到稳定昵称。不同 OpenID 即使前10位摘要碰撞，
    也会逐步使用12、16位等更长摘要；数据库唯一约束负责并发兜底。
    """
    existing = ClientProfile.objects.filter(user=user).first()
    if existing:
        return existing, False

    for nickname in default_nickname_candidates(openid):
        if not nickname_is_available(nickname):
            continue
        try:
            with transaction.atomic():
                profile = ClientProfile.objects.create(
                    user=user,
                    openid=openid,
                    nickname=nickname,
                    nickname_customized=False,
                )
            return profile, True
        except IntegrityError:
            # 同一个微信用户并发登录时，另一请求可能已经完成资料创建。
            existing = ClientProfile.objects.filter(user=user).first()
            if existing:
                return existing, False
            # 不同用户的短摘要极小概率冲突时，继续尝试更长候选。
            continue

    raise RuntimeError('账号初始化失败，请重新登录')
