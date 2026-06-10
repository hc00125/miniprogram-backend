# 俱乐部点单 - 后端 API 文档

> 基础URL：`https://api.huc125.cn`
> 
> 认证方式：除标注 `AllowAny` 的接口外，其余均需在 Header 中携带 JWT Token：
> ```
> Authorization: Bearer <token>
> ```

---

## 一、认证相关 `/api/client/`

### 1.1 微信登录
```
POST /api/client/login
```
**权限**：AllowAny  
**请求体**：
```json
{
  "code": "微信登录 code",
  "nickname": "用户昵称（可选）",
  "avatar_url": "头像URL（可选）"
}
```
**响应**：
```json
{
  "token": "jwt_token",
  "user": {
    "id": 6,
    "username": "wx_xxx",
    "nickname": "test1",
    "avatar_url": "https://...",
    "player_status": "approved"
  }
}
```

### 1.2 账号信息
```
GET /api/client/me
```
**权限**：登录用户  
**响应**：
```json
{
  "id": 6,
  "username": "wx_xxx",
  "nickname": "test1",
  "role": "client",
  "player_status": "approved"
}
```

### 1.3 上传用户头像
```
POST /api/client/avatar
```
**权限**：登录用户  
**请求类型**：`multipart/form-data`  
**字段**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `file` | File | JPG/PNG/WEBP 图片，最大 5MB |

**响应**：
```json
{
  "avatar_url": "https://api.huc125.cn/media/avatars/xxx.jpg",
  "profile": {
    "id": 6,
    "nickname": "test1",
    "avatar_url": "https://api.huc125.cn/media/avatars/xxx.jpg",
    "player_status": "approved"
  }
}
```

---

## 二、陪玩师端 `/api/player/`

### 2.1 申请成为陪玩师
```
POST /api/player/apply
```
**请求体**：
```json
{
  "name": "陪玩师昵称",
  "contact_wechat": "微信号",
  "player_type_id": 1,
  "bio": "简介（可选）"
}
```
**响应**：
```json
{
  "id": 3,
  "status": "pending",
  "name": "陪玩师昵称"
}
```

### 2.2 查看申请状态
```
GET /api/player/apply/status
```
**响应**：
```json
{
  "player_status": "approved",
  "application": { ... },
  "player": { ... }
}
```

### 2.3 陪玩师登录
```
POST /api/player/login
```
**请求体**：
```json
{
  "code": "微信登录 code"
}
```

### 2.4 我的订单（陪玩师视角）
```
GET /api/player/my-orders
```
**说明**：返回该陪玩师抢到的所有订单  
**响应**：
```json
[
  {
    "order_no": "20260604152421BD5F",
    "package_name": "四套娱乐陪",
    "game_id": "企鹅",
    "status": "待支付",
    "total_amount": 15.0,
    "grab_time": "2026-06-04T07:25:33Z",
    "is_designated": false
  }
]
```

### 2.5 抢单大厅
```
GET /api/player/available-orders
```
**说明**：返回所有可抢的订单（状态为"待接单"）  
**响应**：
```json
[
  {
    "order_no": "...",
    "package_name": "...",
    "required_players": 1,
    "current_players": 0,
    "total_price_per_hour": 15.0
  }
]
```

### 2.6 抢单
```
POST /api/player/grab
```
**请求体**：
```json
{
  "order_no": "订单号"
}
```

### 2.7 开始计时
```
POST /api/player/start-timer
```
**请求体**：
```json
{
  "order_no": "订单号"
}
```

### 2.8 完成服务
```
POST /api/player/complete
```
**请求体**：
```json
{
  "order_no": "订单号"
}
```

---

## 三、老板端 `/api/boss/`

### 3.1 创建订单
```
POST /api/boss/order
```
**请求体**：
```json
{
  "boss_wechat": "老板微信ID",
  "game_id": "游戏ID/队伍码（可选）",
  "package_id": 1,
  "required_players": 1,
  "addon_id": 1,
  "addon_details": [{"addon_id": 1, "count": 1}],
  "designated_players": [1, 2],
  "boss_note": "备注（可选）",
  "booked_hours": 1
}
```
**响应**：
```json
{
  "order_no": "20260604152421BD5F",
  "status": "待接单",
  "total_price": 15.0,
  "message": "订单创建成功，等待打手接单"
}
```

### 3.2 我的订单（老板视角）
```
GET /api/boss/orders/me
```
**说明**：返回当前登录用户作为老板的订单  
**响应**：同 2.4

### 3.3 订单详情
```
GET /api/boss/order/<order_no>
```
**响应**：
```json
{
  "order_no": "...",
  "package_name": "...",
  "players": [...],
  "status": "待支付",
  "total_amount": 15.0
}
```

### 3.4 查询套餐
```
GET /api/boss/packages
```
**响应**：
```json
[
  {
    "id": 1,
    "name": "四套娱乐陪",
    "player_count": 1,
    "base_price": 15.0
  }
]
```

### 3.5 查询附加项
```
GET /api/boss/addons
```

### 3.6 查询在线陪玩师
```
GET /api/boss/online-players
```
**响应**：
```json
[
  {
    "id": 1,
    "name": "测试陪玩1",
    "type_name": "女陪",
    "status": "在线"
  }
]
```

---

## 四、支付 `/api/pay/`

### 4.1 微信小程序支付
```
POST /api/pay/wechat/miniprogram/create
```
**请求体**：
```json
{
  "order_no": "订单号",
  "code": "微信登录code"
}
```
**响应（Mock模式）**：
```json
{
  "timeStamp": "1780558143",
  "nonceStr": "8536e4ede67f4b60a544a1d22f435871",
  "package": "prepay_id=mock_PAY...",
  "signType": "RSA",
  "paySign": "xxx"
}
```

### 4.2 模拟支付成功（测试用）
```
POST /api/pay/mock/<payment_no>/success
```
**响应**：
```json
{
  "payment_no": "PAY...",
  "status": "paid"
}
```

### 4.3 查询支付状态
```
GET /api/pay/status/<payment_no>
```

---

## 五、管理员 `/api/admin/`

### 5.1 批准陪玩师申请
```
POST /api/admin/player-applications/<application_id>/approve
```
**请求体**：
```json
{
  "player_type_id": 1,
  "remark": "备注（可选）"
}
```

### 5.2 拒绝陪玩师申请
```
POST /api/admin/player-applications/<application_id>/reject
```
**请求体**：
```json
{
  "reason": "拒绝原因（可选）"
}
```

### 5.3 查看所有申请
```
GET /api/admin/player-applications?status=pending
```

---

## 六、订单状态说明

| status | 说明 |
|--------|------|
| 待接单 | 等待陪玩师抢单 |
| 进行中 | 陪玩师已接单，服务进行中 |
| 待支付 | 服务完成，等待老板付款 |
| 已完成 | 支付完成 |
| 已取消 | 订单已取消 |

---

## 七、常见错误

| HTTP Code | detail | 说明 |
|-----------|--------|------|
| 400 | 身份认证信息未提供 | 未携带 / 格式错误的 Authorization Header |
| 403 | 请先成为陪玩师 | 当前用户不是已批准的陪玩师 |
| 404 | 订单不存在 | 订单号错误 |
| 400 | 订单已支付 | 重复支付 |
