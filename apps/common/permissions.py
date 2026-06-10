from rest_framework.permissions import BasePermission

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
