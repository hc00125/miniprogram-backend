# 当前冠名榜单汇总接口

`GET /api/patronage/wall/` 一次返回全部当前被冠名的公开陪玩及其老板、套餐。匿名只读，不验证或修复传入的登录凭据；POST 等写入方法返回 405。接口不分页、不截断，不需要 `player_id`，不按在线、休息、允许接单或允许指定状态筛人。

响应合同版本为 `1.0`：

```json
{
  "contract_version": "1.0",
  "count": 0,
  "results": []
}
```

`count` 是返回的陪玩数量，成功的空数组表示当前没有符合条件的有效冠名。每个 `results` 条目包含：

|字段|内容|
|---|---|
|`player.id`|真实陪玩 ID，供既有服务详情和冠名页使用|
|`player.name`|当前公开陪玩昵称|
|`player.avatar_url`|公开资料头像；缺资料时为空字符串|
|`player.type_name`|公开陪玩类型名称|
|`crowns`|该陪玩各老板当前有效冠名，沿用既有 `CrownSerializer`|

每条冠名只包含 `id`、`boss_name`、`boss_avatar_url`、`package_name`、`starts_at`、`expires_at`、`source`。时间沿用带时区 ISO8601；`source` 为 `purchase` 或 `day_pass_bonus`。老板缺公开昵称时显示“老板”，缺头像时为空字符串，不使用登录名替代。

查询使用一次服务器时间，并沿用单人冠名接口的资格：购买状态为 `paid`、`starts_at <= now < expires_at`、`revoked_at` 为空。每个 `(player, boss)` 仅保留到期最晚的有效权益；到期时间相同时保留 ID 较大的权益，续冠的历史票据不重复显示。不同老板的权益可以同时展示，包天赠冠仍使用权益记录本身的套餐名称。陪玩须通过 `player_is_available`：已审批、公开可见、未归档，关联账号及公开资料状态满足现有规则。读取不会自动解除账户暂停。

陪玩按未取整的平均评分倒序、ID 升序稳定排列；冠名在每位陪玩下按到期时间和权益 ID 倒序展示。本接口不产生金额排名、名次、额外权益，也不锁定陪玩或订单。

接口通过 `select_related` 在一条 SELECT 中读取权益和相关公开资料，再内存分组，无 N+1 查询。不使用包含联系方式或内部权限的 `PlayerSerializer`，不公开手机号、openid、登录名、token、余额、购买编号、幂等键、金额或收入明细。HTTP 标记 `Cache-Control: no-store`，不新增服务端缓存。

不修改单人 `crowns/`、个人购买记录、报价、支付、授冠、工资或接单流程。不新增表或迁移。回归命令（使用本地隔离测试数据库配置）：

```text
python manage.py test apps.patronage.tests.test_wall apps.patronage.tests.test_crowns apps.patronage.tests.test_records apps.patronage.tests.test_popular_announcements --settings=config.settings_patronage_wall_test
```

该离线配置不加载生产环境文件，使用临时 SQLite 数据库并禁止网络。项目已有迁移包含 PostgreSQL 专用 SQL，离线套件通过同步当前模型 schema 检查读取行为，不执行这些项目迁移；因此它不证明生产 PostgreSQL 迁移兼容性。该接口本身不新增模型或迁移。

测试覆盖有效时段和撤销、未支付、多老板与续冠去重、公开资格、稳定排序、匿名及无效认证头、仅 GET、严格公开字段、固定查询数和无数据库写入。源码接入与测试通过不代表生产已部署；部署后应独立核对新路由再启用前端汇总读取。

前后端合同回放使用真实 Django APIClient 序列化输出，数据为隔离数据库中的虚构陪玩及冠名：

```text
# 在后端仓库执行，须先安装 requirements.txt
python -m apps.patronage.tests.export_wall_fixture /path/to/wall-fixture.json
# 在前端仓库执行，须先安装前端依赖
node tests/patronage-wall-backend.integration.cjs /path/to/wall-fixture.json
```

回放会核对真实前端 parser、共享加载器和面板只发一个汇总 GET，完整显示、缓存和强制刷新，以及服务和冠名的真实 ID 跳转；不会请求线上或支付接口。
