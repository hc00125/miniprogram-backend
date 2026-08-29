from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import ClientProfile
from apps.catalog.models import PlayerType
from apps.common.media_security import ensure_media_publishable

from .models import Player, PlayerApplication


PLAYER_NAME_CONFLICT_MESSAGE = '该昵称已被其他陪玩师使用，请换一个'
PLAYER_NAME_RESERVED_MESSAGE = '该昵称正在被其他申请人使用，请换一个'


def normalize_player_name(value):
    """统一去除首尾空白并压缩连续空白，避免只靠空格制造重复昵称。"""
    return ' '.join(str(value or '').split())


def validate_player_name_available(
    name,
    *,
    user_id=None,
    include_applications=False,
    exclude_application_id=None,
):
    """校验昵称是否可用。

    提交申请时同时检查正式陪玩和待审/已通过申请；管理员审批时再次检查正式陪玩。
    数据库的 ``Player.name`` 唯一约束仍作为并发情况下的最后一道保护。
    """
    normalized = normalize_player_name(name)
    if not normalized:
        raise ValidationError('请输入陪玩昵称')

    players = Player.objects.filter(name__iexact=normalized)
    if user_id:
        players = players.exclude(user_id=user_id)
    if players.exists():
        raise ValidationError(PLAYER_NAME_CONFLICT_MESSAGE)

    if include_applications:
        applications = PlayerApplication.objects.filter(
            name__iexact=normalized,
            status__in=[
                PlayerApplication.STATUS_PENDING,
                PlayerApplication.STATUS_APPROVED,
            ],
        )
        if exclude_application_id:
            applications = applications.exclude(pk=exclude_application_id)
        if user_id:
            applications = applications.exclude(user_id=user_id)
        if applications.exists():
            raise ValidationError(PLAYER_NAME_RESERVED_MESSAGE)

    return normalized


@transaction.atomic
def approve_player_application(
    application_id,
    reviewer,
    *,
    player_type_id=None,
    remark=None,
):
    """把一条申请原子地转成正式陪玩档案。

    正式陪玩创建、申请状态和客户状态必须全部成功才提交；任何一步失败都会回滚，
    从而避免 ``player_status=approved`` 但没有 ``Player`` 记录的半成功数据。
    Django Admin 与管理端 API 必须统一调用本函数，不能各自复制审批逻辑。
    """
    application = (
        PlayerApplication.objects
        .select_for_update(of=('self',))
        .select_related('user', 'player_type')
        .get(pk=application_id)
    )
    if not application.user_id:
        raise ValidationError('申请未绑定用户，无法通过审核')

    # 音频属于异步内容安全检测：只有微信最终返回 pass 才允许进入公开陪玩资料。
    ensure_media_publishable(
        user=application.user,
        media_url=application.audio_intro_url,
    )

    # 审批时再次检查正式陪玩和其他待审/已通过申请，兼容修复前遗留的同名数据。
    name = validate_player_name_available(
        application.name,
        user_id=application.user_id,
        include_applications=True,
        exclude_application_id=application.id,
    )
    if player_type_id:
        player_type = PlayerType.objects.filter(id=player_type_id, is_active=True).first()
        if not player_type:
            raise ValidationError('陪玩类型不存在或已停用')
    else:
        player_type = application.player_type or PlayerType.objects.filter(
            is_active=True,
        ).order_by('priority', 'id').first()
    if not player_type:
        raise ValidationError('没有可用的陪玩类型，请先在后台启用陪玩类型')

    try:
        player, _ = Player.objects.update_or_create(
            user=application.user,
            defaults={
                'name': name,
                'player_type': player_type,
                'contact_wechat': application.contact_wechat,
                'bio': application.bio or '',
                'audio_intro_url': application.audio_intro_url or '',
                'audio_intro_title': application.audio_intro_title or '',
                'status': Player.STATUS_APPROVED,
            },
        )
    except IntegrityError as exc:
        # 处理两个管理员并发审核同名申请时的数据库唯一约束竞争。
        raise ValidationError(PLAYER_NAME_CONFLICT_MESSAGE) from exc

    reviewed_at = timezone.now()
    application.name = name
    application.player_type = player_type
    application.status = PlayerApplication.STATUS_APPROVED
    application.reviewed_at = reviewed_at
    application.reviewed_by = reviewer
    update_fields = [
        'name', 'player_type', 'status', 'reviewed_at', 'reviewed_by',
    ]
    if remark is not None:
        application.remark = remark
        update_fields.append('remark')
    application.save(update_fields=update_fields)

    updated_profiles = ClientProfile.objects.filter(user=application.user).update(
        player_status=ClientProfile.PLAYER_STATUS_APPROVED,
        updated_at=reviewed_at,
    )
    if updated_profiles != 1:
        raise ValidationError('客户资料不存在，无法完成陪玩师审核')

    return player, application
