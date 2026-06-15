# 微信小程序支付接入说明

本分支使用微信支付 API v3 的普通商户 JSAPI/小程序支付模式。

## 支付接口

- POST /api/pay/wechat/miniprogram/create
- POST /api/pay/wechat/query/<payment_no>
- POST /api/pay/wechat/callback

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
