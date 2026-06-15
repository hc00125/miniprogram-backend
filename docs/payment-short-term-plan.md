# 小程序支付模块短期优化计划

> 当前支付主线为 `feat/wechat-pay-v3`。本计划以该分支现有微信支付 V3 实现为基础，不替换其登录校验、订单归属校验、主动查单、回调验签、金额校验和测试能力。

## 当前后端现状

- 技术栈：Django + Django REST Framework。
- 订单模块：`apps.orders` 已有订单、接单、完成、取消等能力。
- 支付模块：`apps.payments` 已有微信小程序 JSAPI 支付、主动查单、微信支付回调验签与解密、金额校验、模拟支付开关。
- 本分支已补充：退款记录模型、后台支付流水查询、后台回调日志查询、后台退款查询与退款记录创建。

## 短期计划表

| 优先级 | 模块 | 要做的事 | 当前状态 | 验收标准 |
| --- | --- | --- | --- | --- |
| P0 | 微信支付 | 保留 `feat/wechat-pay-v3` 的 JSAPI 下单、签名、回调验签、主动查单 | 已完成 | 小程序可拉起支付，后端可确认最终支付状态 |
| P0 | 安全校验 | 支付接口要求登录，校验订单归属，回调校验 appid、mchid、金额、订单号 | 已完成 | 非订单本人不能发起支付；异常回调不能确认订单 |
| P1 | 后台管理 | 后台增加支付流水、支付状态筛选、支付详情、回调日志查看 | 已完成接口 | 管理员能查支付失败和异常订单 |
| P1 | 退款记录 | 增加退款申请记录、退款中、退款成功、退款失败状态 | 已完成本地记录 | 后台能创建和查询退款记录 |
| P1 | 真实退款 | 接入微信支付退款 API 和退款回调 | 待做 | 后台创建退款后可真实原路退回 |
| P1 | 对账 | 增加按日期统计：订单金额、支付金额、退款金额、异常订单 | 待做 | 每日能核对流水是否一致 |

## 新增后台接口

- `GET /api/admin/payments`：支付流水列表，支持 `status/channel/scene/order_no/payment_no` 查询参数。
- `GET /api/admin/payments/<payment_no>`：支付详情。
- `GET /api/admin/payment-callbacks`：支付回调日志，支持 `payment_no/channel/verified` 查询参数。
- `GET /api/admin/refunds`：退款列表，支持 `status/payment_no/order_no` 查询参数。
- `POST /api/admin/refunds`：后台创建退款记录。
- `GET /api/admin/refunds/<refund_no>`：退款详情。

## 真实联调前检查

1. 复制 `.env.example` 为 `.env`，填写微信支付商户配置。
2. 确保生产环境 `ENABLE_MOCK_PAYMENT=false`。
3. 运行迁移：`python manage.py migrate`。
4. 运行检查和测试：

```bash
python manage.py check
python manage.py test apps.payments
```

## 下一批建议改动

1. 将本地 `Refund` 继续接入微信支付退款 API。
2. 新增退款回调验签、解密、状态更新。
3. 后台订单详情增加支付记录和退款记录嵌套展示。
4. 新增每日对账接口。
