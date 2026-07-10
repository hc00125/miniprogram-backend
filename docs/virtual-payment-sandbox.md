# 微信小程序虚拟支付沙箱测试

本项目已接入微信官方 `wx.requestVirtualPayment` 的道具直购模式：

- `mode`: `short_series_goods`
- `env`: `1`（沙箱）
- 首个测试道具：`escort_15`
- 单价：`1500` 分（15 元）

## 1. 后端环境变量

在服务器 `.env` 中填写：

```env
WECHAT_VIRTUALPAY_ENABLED=true
WECHAT_VIRTUALPAY_ENV=1
WECHAT_VIRTUALPAY_OFFER_ID=你的OfferID
WECHAT_VIRTUALPAY_APP_KEY=你的沙箱AppKey

# 首次测试的兜底配置
WECHAT_VIRTUALPAY_SANDBOX_PRODUCT_ID=escort_15
WECHAT_VIRTUALPAY_SANDBOX_PRICE_FEN=1500
WECHAT_VIRTUALPAY_SANDBOX_PACKAGE_KEYWORD=四套四弹

# 禁止旧的模拟普通支付入口
ENABLE_MOCK_PAYMENT=false
```

注意：沙箱 AppKey 只能配置在后端，不能写入小程序前端或提交到 GitHub。

## 2. 数据库迁移

```bash
python manage.py migrate
python manage.py check
```

迁移后，Django 后台会新增：

```text
支付管理 → 虚拟支付商品绑定
```

建议创建正式绑定：

```text
微信道具ID：escort_15
道具单价（分）：1500
绑定商品：四套四弹15元商品
绑定规格：留空
是否启用：是
```

如果四套四弹15元是某个商品下的规格，则选择“绑定规格”，并将“绑定商品”留空。商品和规格只能选择一个。

没有建立绑定时，沙箱环境会临时使用环境变量兜底，但只允许：

- 单个商品
- 商品名称包含“四套四弹”
- 单价15元
- 订单总金额等于15元 × 数量
- 不包含附加项、指定陪玩费用或其他动态金额

## 3. 前端构建

```bash
npm run build:mp-weixin
```

前端支付页仍调用原来的 `uni.requestPayment`，运行时桥接器会识别后端返回的 `virtual_payment:` 参数并自动改调微信原生：

```js
wx.requestVirtualPayment({
  signData,
  paySig,
  signature,
  mode: 'short_series_goods'
})
```

支付成功后，前端会轮询后端；后端通过 `/xpay/query_order` 向微信确认支付状态和金额，确认无误后才把本地订单标记为已支付，并调用 `/xpay/notify_provide_goods` 完成发货确认。

## 4. 沙箱测试准备

1. 微信虚拟支付后台中的 `escort_15` 保持在“开发版本”。
2. 道具审核通过后，不需要先发布现网即可进行沙箱测试。
3. 使用与该小程序绑定的微信账号登录。
4. 建议使用真机预览或体验版测试，微信基础库需支持 `requestVirtualPayment`。
5. 后端必须能访问 `api.weixin.qq.com`。
6. 小程序请求域名中必须配置后端 HTTPS 域名。

## 5. 首次测试订单

首次只测试以下订单：

```text
商品：四套四弹15元
数量：1
总金额：15元
附加项：无
指定陪玩：无
合并商品：无
```

让陪玩完成服务并把订单推进到“待支付”，老板进入支付页点击微信支付。

## 6. 成功标准

支付成功后检查：

1. 小程序订单状态变为“已完成”。
2. `payment_transactions` 中出现：
   - `channel=wechat_virtual`
   - `scene=short_series_goods`
   - `status=paid`
3. 支付记录的 `notify_payload.query_order` 中有微信订单信息。
4. 微信虚拟支付后台“交易订单”能查到对应订单。
5. 重复点击支付不会重复完成同一张本地订单。

## 7. 常见错误

- `-15005`：用户态签名错误。重新登录后再试，确认后端使用本次 `wx.login` 换取的 `session_key`。
- `-15006`：支付签名错误。检查沙箱 AppKey 和 OfferID。
- `-15010`：道具未发布。现网 `env=0` 才要求现网发布；沙箱测试确认 `env=1` 且道具存在于开发版本。
- `-15011`：现网版本不能使用沙箱环境。说明正在用正式发布的小程序包测试 `env=1`，请使用开发版/体验版，或切换现网配置。
- `-15013`：道具价格错误。确认 `escort_15` 后台价格为15元，后端配置为1500分。
- `-15016`：`signData` 格式错误。检查后端是否部署了当前版本代码。

## 8. 切换现网

沙箱全部通过、道具发布现网后，服务器改为：

```env
WECHAT_VIRTUALPAY_ENV=0
WECHAT_VIRTUALPAY_APP_KEY=你的现网AppKey
```

重启后端后再用体验版做一次现网验证。不要把沙箱 AppKey 和现网 AppKey 同时写入前端。
