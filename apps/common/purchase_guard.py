from django.conf import settings
from rest_framework.exceptions import APIException
from rest_framework.permissions import BasePermission


IOS_PURCHASE_DISABLED_MESSAGE = 'iOS端当前暂不提供在线购买'


class IOSPurchaseDisabled(APIException):
    status_code = 403
    default_code = 'ios_purchase_disabled'

    def __init__(self):
        super().__init__({
            'detail': IOS_PURCHASE_DISABLED_MESSAGE,
            'code': 'IOS_PURCHASE_DISABLED',
        })


def request_client_platform(request):
    explicit = str(request.headers.get('X-Client-Platform') or '').strip().lower()
    if explicit in {'ios', 'iphone', 'ipad', 'ipod'}:
        return 'ios'
    if explicit == 'android':
        return 'android'

    user_agent = str(request.headers.get('User-Agent') or '').lower()
    if any(token in user_agent for token in ('iphone', 'ipad', 'ipod')):
        return 'ios'
    if 'android' in user_agent:
        return 'android'
    return explicit or 'other'


def ios_purchase_disabled(request):
    return not settings.IOS_PURCHASE_ENABLED and request_client_platform(request) == 'ios'


class IsPurchaseAvailable(BasePermission):
    """禁止 iOS 创建订单、充值单或支付单；查询、取消及售后接口不受影响。"""

    def has_permission(self, request, view):
        if ios_purchase_disabled(request):
            raise IOSPurchaseDisabled()
        return True


def require_purchase_available(view):
    """给现有 DRF 函数视图追加购买渠道校验，不改变原认证与账户权限。"""
    permission_classes = list(getattr(view.cls, 'permission_classes', []))
    if IsPurchaseAvailable not in permission_classes:
        permission_classes.append(IsPurchaseAvailable)
    view.cls.permission_classes = permission_classes
    return view
