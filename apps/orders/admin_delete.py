import os

from django.conf import settings


_TRUE_VALUES = {'1', 'true', 'yes', 'on'}


def order_admin_delete_enabled():
    """返回 Django Admin 是否允许物理删除订单。

    订单删除仅用于开发/测试数据清理，必须同时满足：
    1. Django DEBUG=True；
    2. 环境变量 ADMIN_ORDER_DELETE_ENABLED=true。

    使用双重条件可以避免正式环境仅因误配一个开关而开放订单删除。
    """
    explicit_switch = os.environ.get('ADMIN_ORDER_DELETE_ENABLED', 'false').lower() in _TRUE_VALUES
    return bool(settings.DEBUG and explicit_switch)


def can_delete_orders_in_admin(request):
    """只有开发删除开关开启时，超级管理员才可以删除订单及其内部关联记录。"""
    return bool(
        getattr(request.user, 'is_superuser', False)
        and order_admin_delete_enabled()
    )
