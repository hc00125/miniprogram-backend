# P2 发布摘要

## 已完成

- 老板累计消费改为不可篡改流水计算；
- 已完成订单自动增加消费，成功退款自动扣减；
- 支持后台人工正负调整，必须填写原因并记录管理员；
- 默认四档VIP，可在后台自由修改门槛、名称、颜色和权益；
- 小程序账号页展示VIP、累计消费、权益和升级进度；
- 后台消费查询页展示VIP、订单和流水；
- 建立运营、财务、客服、内容和只读审计五类最小权限角色；
- 提供安全创建/轮换独立管理员账号的命令；
- 提供历史消费补录、P2健康检查、备份、部署和服务器迁移手册；
- 商品列表使用统一品牌封面组件，无图片时自动生成主题封面；
- 提供商品素材规范和上架审计命令。

## 部署后必须执行

```bash
python manage.py migrate
python manage.py bootstrap_admin_roles --clear
python manage.py backfill_boss_consumption
python manage.py audit_product_assets
python manage.py p2_healthcheck
```

前端必须重新构建并上传微信体验版：

```bash
npm ci
npm run build:mp-weixin
```
