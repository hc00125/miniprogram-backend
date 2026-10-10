from .permissions import IsAccountOperational


def require_operational(view):
    """给现有 DRF 函数视图追加账户状态校验，不改动其原认证权限。"""
    permission_classes = list(getattr(view.cls, 'permission_classes', []))
    if IsAccountOperational not in permission_classes:
        permission_classes.append(IsAccountOperational)
    view.cls.permission_classes = permission_classes
    return view
