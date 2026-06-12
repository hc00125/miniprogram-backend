# 微信小程序支付接入说明

本分支使用微信支付 API v3 的普通商户 JSAPI/小程序支付模式。Django 后端负责统一下单、RSA 签名、支付回调验签与解密、主动查单和订单状态更新；小程序端只调用 `wx.requestPayment`。

## 必需配置

复制 `.env.example` 为 `.env`，填写：

- `WECHAT_APP_ID`：已绑定商户号的小程序 AppID。
- `WECHAT_APP_SECRET`：小程序登录换取 OpenID 使用。
- `WECHATPAY_MCH_ID`：微信支付商户号。
- `WECHATPAY_MERCHANT_SERIAL_NO`：商户 API 证书序列号。
- `WECHATPAY_MERCHANT_PRIVATE_KEY_PATH`：商户私钥 `apiclient_key.pem` 的服务器路径。
- `WECHATPAY_API_V3_KEY`：商户平台设置的 32 字节 APIv3 密钥。
- `WECHATPAY_PUBLIC_KEY_ID`：微信支付公钥 ID，格式为 `PUB_KEY_ID_...`。
- `WECHATPAY_PUBLIC_KEY_PATH`：从商户平台下载的微信支付公钥 PEM 文件路径。
- `WECHATPAY_NOTIFY_URL`：公网 HTTPS 回调地址，例如 `https://api.example.com/api/pay/wechat/callback`。
- `ENABLE_MOCK_PAYMENT=false`：生产环境必须关闭模拟支付。

私钥、APIv3 密钥和 AppSecret 不得提交到 Git。建议把密钥文件放在项目目录外，并仅允许运行 Django 的系统用户读取。

## 后端接口

### 创建支付

`POST /api/pay/wechat/miniprogram/create`

请求头：

```text
Authorization: Bearer <小程序登录后取得的JWT>
Content-Type: application/json
```

请求体：

```json
{
  "order_no": "业务订单号"
}
```

后端从当前登录用户的 `ClientProfile.openid` 获取付款人 OpenID，不使用前端传入的真实支付 OpenID。返回值可直接传给 `wx.requestPayment`。

### 主动查单

`POST /api/pay/wechat/query/<payment_no>`

小程序从 `wx.requestPayment` 返回后应调用该接口确认最终支付状态。不能只根据前端 `success` 回调把订单标记为已支付。

### 支付通知

`POST /api/pay/wechat/callback`

微信支付服务器调用。后端会：

1. 使用微信支付公钥验证回调签名；
2. 使用 APIv3 密钥解密 `resource`；
3. 校验 AppID、商户号、支付单号、币种和金额；
4. 使用数据库行锁幂等更新支付单与业务订单。

## 小程序调用示例

```javascript
async function payOrder(orderNo) {
  const payParams = await request({
    url: '/api/pay/wechat/miniprogram/create',
    method: 'POST',
    data: { order_no: orderNo }
  })

  try {
    await new Promise((resolve, reject) => {
      wx.requestPayment({
        timeStamp: payParams.timeStamp,
        nonceStr: payParams.nonceStr,
        package: payParams.package,
        signType: payParams.signType,
        paySign: payParams.paySign,
        success: resolve,
        fail: reject
      })
    })
  } finally {
    return request({
      url: `/api/pay/wechat/query/${payParams.payment_no}`,
      method: 'POST'
    })
  }
}
```

## 部署检查

```bash
pip install -r requirements.txt
python manage.py check
```

回调地址必须公网可访问、使用 HTTPS、无需登录、没有额外跳转，并且反向代理必须原样转发请求体和 `Wechatpay-*` 请求头。

官方文档：

- 小程序调起支付：`https://developers.weixin.qq.com/miniprogram/dev/api/payment/wx.requestPayment.html`
- JSAPI/小程序下单：`https://pay.wechatpay.cn/doc/v3/merchant/4012791856`
- JSAPI 调起支付：`https://pay.wechatpay.cn/doc/v3/merchant/4012791857`
- 支付成功回调：`https://pay.wechatpay.cn/doc/v3/merchant/4012791861`
