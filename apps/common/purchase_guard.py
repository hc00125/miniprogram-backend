import os

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

    user_agent = str(request.headers.get('User-Agent') or '').lower()
    if any(token in user_agent for token in ('iphone', 'ipad', 'ipod')):
        return 'ios'
    if explicit == 'android' or 'android' in user_agent:
        return 'android'
    if explicit in {'windows', 'win32', 'win'}:
        return 'windows'
    if explicit in {'harmony', 'harmonyos', 'ohos'}:
        return 'harmony'
    return explicit or 'other'


def ios_purchase_enabled():
    configured = getattr(settings, 'IOS_PURCHASE_ENABLED', None)
    if configured is not None:
        return bool(configured)
    # 2026 起微信小程序虚拟支付已支持 iOS。默认开启，仍可通过环境变量
    # IOS_PURCHASE_ENABLED=false 一键回滚/临时熔断。
    return os.environ.get('IOS_PURCHASE_ENABLED', 'true').lower() in {'1', 'true', 'yes', 'on'}


def ios_purchase_disabled(request):
    return not ios_purchase_enabled() and request_client_platform(request) == 'ios'


class IsPurchaseAvailable(BasePermission):
    """购买渠道总开关；默认允许 iOS，必要时可通过环境变量临时关闭。"""

    def has_permission(self, request, view):
        if ios_purchase_disabled(request):
            raise IOSPurchaseDisabled()
        return True


class AllowIOSBalancePayment(BasePermission):
    """钱包余额付款始终可用；保留此权限类兼容现有路由。"""

    def has_permission(self, request, view):
        return True


def require_purchase_available(view):
    """给现有 DRF 函数视图追加购买渠道校验，不改变原认证与账户权限。"""
    permission_classes = list(getattr(view.cls, 'permission_classes', []))
    if IsPurchaseAvailable not in permission_classes:
        permission_classes.append(IsPurchaseAvailable)
    view.cls.permission_classes = permission_classes
    return view


def require_ios_balance_only(view):
    """余额支付专用：iOS 与其他端统一放行，保留旧接口包装层。"""
    permission_classes = list(getattr(view.cls, 'permission_classes', []))
    if IsPurchaseAvailable in permission_classes:
        permission_classes.remove(IsPurchaseAvailable)
    if AllowIOSBalancePayment not in permission_classes:
        permission_classes.append(AllowIOSBalancePayment)
    view.cls.permission_classes = permission_classes
    return view
