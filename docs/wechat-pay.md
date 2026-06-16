# 微信小程序支付接入说明

本分支使用微信支付 API v3 的普通商户 JSAPI/小程序支付模式。

## 支付接口

- POST /api/pay/wechat/miniprogram/create
- POST /api/pay/wechat/query/<payment_no>
- POST /api/pay/wechat/callback

## 关单逻辑

- 后端已封装微信支付关单能力。
- 本地支付单状态为 created 或 paying 时可以关闭。
- 订单取消入口会先尝试关闭未支付支付单，再取消业务订单。
- 生产环境关闭微信支付单时会调用微信关单接口；开发模拟支付模式只关闭本地支付单。

## 手动确认支付

- 手动确认支付后，订单会从待支付改为已完成。
- 系统会设置 paid=true、payment_method=self_confirm、payment_confirmed_at，并写入订单状态日志。

## 管理后台接口

- GET /api/admin/payments
- GET /api/admin/payments/<payment_no>
- GET /api/admin/payment-callbacks
- GET /api/admin/refunds
- POST /api/admin/refunds
- GET /api/admin/refunds/<refund_no>

当前退款能力是本地退款记录，还没有调用微信支付退款 API。

## 部署检查

运行：

python manage.py migrate
python manage.py check
python manage.py test apps.payments
