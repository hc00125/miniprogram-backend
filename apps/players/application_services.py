from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import ClientProfile

from .models import Player, PlayerApplication


class PlayerApplicationApprovalError(Exception):
    """陪玩申请无法安全通过时抛出的业务异常。"""


def normalize_player_name(value):
    """统一去除首尾空白并折叠连续空格，避免视觉相同名称绕过校验。"""
    return ' '.join(str(value or '').strip().split())


def player_name_conflict_message(name, *, exclude_user_id=None, include_applications=False):
    """返回名称占用提示；没有冲突时返回空字符串。"""
    normalized = normalize_player_name(name)
    if not normalized:
        return '请输入陪玩昵称'

    players = Player.objects.filter(name__iexact=normalized)
    if exclude_user_id:
        players = players.exclude(user_id=exclude_user_id)
    if players.exists():
        return '该昵称已被其他陪玩师使用，请换一个'

    if include_applications:
        applications = PlayerApplication.objects.filter(
            name__iexact=normalized,
            status__in=[
                PlayerApplication.STATUS_PENDING,
                PlayerApplication.STATUS_APPROVED,
            ],
        )
        if exclude_user_id:
            applications = applications.exclude(user_id=exclude_user_id)
        if applications.exists():
            return '该昵称正在被其他用户申请，请换一个'

    return ''


@transaction.atomic
def approve_player_application(application, reviewer, *, player_type=None, remark=None):
    """原子化完成审批：先确保正式陪玩档案成功，再同步申请和客户状态。

    单条审批、批量审批和管理端 API 必须统一调用这里，避免出现
    ``player_status=approved`` 但正式 ``Player`` 不存在的半成功数据。
    """
    if not application or not application.pk:
        raise PlayerApplicationApprovalError('申请记录不存在')

    # 锁定申请，避免两个管理员同时审批同一条记录。
    source_values = {
        'name': normalize_player_name(application.name),
        'real_name': application.real_name,
        'contact_wechat': application.contact_wechat,
        'bio': application.bio or '',
        'audio_intro_url': application.audio_intro_url or '',
        'audio_intro_title': application.audio_intro_title or '',
    }
    locked = (
        PlayerApplication.objects
        .select_for_update()
        .select_related('user', 'player_type')
        .get(pk=application.pk)
    )

    selected_type = player_type or application.player_type or locked.player_type
    if not selected_type:
        raise PlayerApplicationApprovalError('请选择陪玩类型')
    if not selected_type.is_active:
        raise PlayerApplicationApprovalError('所选陪玩类型已停用，请重新选择')

    conflict = player_name_conflict_message(
        source_values['name'],
        exclude_user_id=locked.user_id,
        include_applications=False,
    )
    if conflict:
        raise PlayerApplicationApprovalError(conflict)

    defaults = {
        'name': source_values['name'],
        'player_type': selected_type,
        'contact_wechat': source_values['contact_wechat'],
        'bio': source_values['bio'],
        'audio_intro_url': source_values['audio_intro_url'],
        'audio_intro_title': source_values['audio_intro_title'],
        'status': Player.STATUS_APPROVED,
    }

    # 数据库 unique=True 仍作为并发情况下的最后一道保护；使用内层保存点，
    # 即使触发 IntegrityError，外层事务也能保持可用并完整回滚。
    try:
        with transaction.atomic():
            player, created = Player.objects.select_for_update().get_or_create(
                user=locked.user,
                defaults=defaults,
            )
            if not created:
                for field, value in defaults.items():
                    setattr(player, field, value)
                player.save(update_fields=[*defaults.keys(), 'updated_at'])
    except IntegrityError as exc:
        raise PlayerApplicationApprovalError(
            '该昵称已被其他陪玩师使用，请让申请人修改后重新审核'
        ) from exc

    locked.name = source_values['name']
    locked.real_name = source_values['real_name']
    locked.contact_wechat = source_values['contact_wechat']
    locked.bio = source_values['bio']
    locked.audio_intro_url = source_values['audio_intro_url']
    locked.audio_intro_title = source_values['audio_intro_title']
    locked.player_type = selected_type
    locked.status = PlayerApplication.STATUS_APPROVED
    locked.reject_reason = ''
    if remark is not None:
        locked.remark = remark
    locked.reviewed_by = reviewer
    locked.reviewed_at = timezone.now()
    locked.save(update_fields=[
        'name', 'real_name', 'contact_wechat', 'bio',
        'audio_intro_url', 'audio_intro_title', 'player_type',
        'status', 'reject_reason', 'remark', 'reviewed_by', 'reviewed_at',
    ])

    ClientProfile.objects.filter(user=locked.user).update(
        player_status=ClientProfile.PLAYER_STATUS_APPROVED,
        updated_at=timezone.now(),
    )
    return locked, player
