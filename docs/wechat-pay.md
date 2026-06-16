# 微信小程序支付接入说明

本分支使用微信支付 API v3 的普通商户 JSAPI/小程序支付模式。

## 支付接口

- POST /api/pay/wechat/miniprogram/create
- POST /api/pay/wechat/query/<payment_no>
- POST /api/pay/wechat/callback

## 订单接口安全

- 订单详情、取消订单、评价、手动确认支付都需要登录。
- 普通用户只能操作自己的订单。
- 管理员可以查看和操作所有订单。
- 手动确认支付在生产环境仅允许管理员使用。
- 普通用户支付应走微信支付回调或主动查单确认。

## 登录安全

- 生产环境不允许前端直接传 openid 登录。
- 生产环境必须使用 wx.login code 换取 openid。
- 只有 DEBUG=true 或 ENABLE_DEV_OPENID_LOGIN=true 时，才允许开发调试直传 openid。

## 关单逻辑

- 后端已封装微信支付关单能力。
- 本地支付单状态为 created 或 paying 时可以关闭。
- 订单取消入口会先尝试关闭未支付支付单，再取消业务订单。
- 生产环境关闭微信支付单时会调用微信关单接口；开发模拟支付模式只关闭本地支付单。

## 手动确认支付

- 手动确认支付后，订单会从待支付改为已完成。
- 系统会设置 paid=true、payment_method=self_confirm、payment_confirmed_at，并写入订单状态日志。
- 生产环境仅管理员可用，普通老板端不应展示该入口。

## 管理后台接口

- GET /api/admin/payments
- GET /api/admin/payments/<payment_no>
- GET /api/admin/payment-callbacks
- GET /api/admin/refunds
- POST /api/admin/refunds
- GET /api/admin/refunds/<refund_no>

当前退款能力是本地退款记录，还没有调用微信支付退款 API。前端没有退款入口时，可以先保留后端预留能力。

## 生产配置检查

运行 python manage.py check 时，会检查生产环境危险配置，包括：

- ALLOWED_HOSTS 不能是 *
- 不能开启 CORS 全开放
- 不能开启 mock 支付
- 不能开启开发 openid 登录
- 必须配置微信登录和微信支付必要参数
- 不能使用默认 SECRET_KEY

## 部署检查

python manage.py migrate
python manage.py check
python manage.py test apps.orders apps.payments
