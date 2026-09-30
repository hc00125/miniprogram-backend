from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.accounts.access import ensure_account_operational
from apps.players.models import Player


def current_player(user):
    player = getattr(user, 'legacy_player', None)
    if player:
        return player
    if getattr(user, 'is_authenticated', False):
        try:
            return user.player_profile
        except Player.DoesNotExist:
            return None
    return None


class IsAdminUser(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_staff)


class IsAccountOperational(BasePermission):
    """允许查询和售后读取；对受限账户阻止新增/修改业务操作。"""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        ensure_account_operational(request.user)
        return True


class IsApprovedPlayer(BasePermission):
    message = '请先成为陪玩师'

    def has_permission(self, request, view):
        player = current_player(request.user)
        if not player:
            return False
        if player.status == Player.STATUS_PENDING:
            self.message = '陪玩师申请审核中'
            return False
        if player.status == Player.STATUS_REJECTED:
            self.message = '陪玩师申请未通过'
            return False
        if player.status == Player.STATUS_DISABLED:
            self.message = '陪玩师账号已禁用'
            return False
        return player.status == Player.STATUS_APPROVED
