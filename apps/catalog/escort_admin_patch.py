from django.contrib import admin

from .models import Package


def _insert_after(values, anchor, value):
    result = list(values or [])
    if value in result:
        return result
    try:
        index = result.index(anchor) + 1
    except ValueError:
        index = len(result)
    result.insert(index, value)
    return result


def patch_package_admin():
    """把护航资格开关加入现有商品后台，不改动商品端页面结构。"""
    model_admin = admin.site._registry.get(Package)
    if not model_admin:
        return

    field_name = 'requires_escort_qualification'
    model_admin.list_display = _insert_after(model_admin.list_display, 'product_type', field_name)
    model_admin.list_filter = _insert_after(model_admin.list_filter, 'product_type', field_name)
    model_admin.list_editable = _insert_after(model_admin.list_editable, 'player_count', field_name)

    updated_fieldsets = []
    for title, options in model_admin.fieldsets or []:
        next_options = dict(options)
        if title == '常用配置':
            next_options['fields'] = _insert_after(next_options.get('fields', []), 'product_type', field_name)
            next_options['description'] = (
                '日常上架主要填这里。“需要护航资格”开启后，订单仍使用原接单流程，'
                '但只有护航资格已通过且陪玩等级满足要求的人员可以接单。'
            )
        updated_fieldsets.append((title, next_options))
    model_admin.fieldsets = updated_fieldsets
