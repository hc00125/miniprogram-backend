# Django Admin 订单删除开关

订单物理删除仅用于开发和测试环境。正式部署环境必须保持关闭。

## 开发/测试环境

```env
DJANGO_DEBUG=true
ADMIN_ORDER_DELETE_ENABLED=true
```

只有 Django 超级管理员可以在后台单个或批量删除订单。订单护航快照、指定邀请、订单商品等 `CASCADE` 关联记录会随订单删除；退款、工资、续单等 `PROTECT` 关联仍会继续阻止删除。

## 正式环境

```env
DJANGO_DEBUG=false
ADMIN_ORDER_DELETE_ENABLED=false
```

代码使用双重保护：即使误将 `ADMIN_ORDER_DELETE_ENABLED=true`，只要 `DJANGO_DEBUG=false`，订单删除仍不会开放。

修改环境变量后需重启 Django/Gunicorn 进程，使配置重新加载。
