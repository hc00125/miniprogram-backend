from django.utils import timezone
from rest_framework.permissions import BasePermission
from .secrets import KookError

class ApprovedWechatPlayer(BasePermission):
    def has_permission(self, request, view):
        profile = getattr(request.user, 'client_profile', None)
        if not profile or not profile.openid:
            raise KookError('JWT_WECHAT_REQUIRED', 403)
        player = getattr(request.user, 'player_profile', None)
        if not player or player.status != 'approved':
            raise KookError('PLAYER_NOT_APPROVED', 403)
        # Pure read: do not call the existing helper which mutates expiry state.
        status = profile.account_status
        if status != 'active' and not (status == 'suspended' and profile.account_suspended_until and profile.account_suspended_until <= timezone.now()):
            raise KookError('ACCOUNT_RESTRICTED', 403)
        request.kook_player = player
        return True
