# P2 服务器迁移与上线手册

本文适用于后端分支 `feat/wechat-pay-v3` 与前端分支 `feat/product-order-management`。

## 1. 上线前准备

1. 在云厂商控制台创建整机快照。
2. 备份数据库、媒体文件、环境变量、Nginx 配置和支付证书。
3. 确认服务器工作区没有未提交改动：

```bash
git status --short
git branch --show-current
```

4. 记录当前可回滚提交：

```bash
git rev-parse HEAD > /tmp/backend-before-p2.sha
```

## 2. 推荐备份命令

先配置：

```bash
export BACKUP_ROOT=/var/backups/touchi
export DATABASE_URL='postgresql://...'
```

再执行仓库中的：

```bash
bash scripts/backup_p2.sh
```

备份产物应至少包括：

- PostgreSQL 自定义格式备份；
- `MEDIA_ROOT` 压缩包；
- `.env`、Nginx、systemd 配置副本；
- 当前 Git 提交号；
- SHA256 校验文件。

## 3. 部署顺序

```bash
git checkout feat/wechat-pay-v3
git pull --ff-only
source .venv/bin/activate
pip install -r requirements.txt
python manage.py check
python manage.py migrate --plan
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py bootstrap_admin_roles --clear
python manage.py backfill_boss_consumption
python manage.py p2_healthcheck
```

然后按实际服务名重启：

```bash
sudo systemctl restart touchi-gunicorn
sudo systemctl reload nginx
```

不要直接复制示例服务名。先执行：

```bash
systemctl list-units --type=service | grep -E 'gunicorn|django|uwsgi|touchi'
```

## 4. 创建独立管理员账号

禁止多人共用一个超级管理员账号。示例：

```bash
export ADMIN_INITIAL_PASSWORD='一次性强密码'
python manage.py provision_admin --username ops01 --role 运营管理员
python manage.py provision_admin --username finance01 --role 财务管理员
python manage.py provision_admin --username service01 --role 客服管理员
unset ADMIN_INITIAL_PASSWORD
```

可用角色：

- 运营管理员；
- 财务管理员；
- 客服管理员；
- 内容管理员；
- 只读审计。

超级管理员只用于权限、系统和紧急处理：

```bash
export ADMIN_INITIAL_PASSWORD='一次性强密码'
python manage.py provision_admin --username root-admin --superuser --rotate-password
unset ADMIN_INITIAL_PASSWORD
```

## 5. P2 数据验证

进入 Django 后台检查：

1. `客户管理 → 老板VIP等级`：默认四档均存在；
2. `客户管理 → 老板消费流水`：历史订单、退款均已补齐；
3. 客户资料列表显示累计消费与VIP；
4. 人工增加或扣减消费时必须填写原因；
5. 退款成功后累计消费只扣减一次；
6. 小程序账号页显示VIP、权益和升级进度；
7. 财务、客服、内容账号只能看到各自权限范围。

## 6. 前端发布

在构建电脑执行：

```bash
git checkout feat/product-order-management
git pull --ff-only
npm ci
npm run build:mp-weixin
```

使用微信开发者工具打开 `dist/build/mp-weixin`，完成体验版回归后上传。后端上线不会自动更新小程序前端。

## 7. 回滚

出现严重问题时：

1. 立即停止新支付入口或切换维护页；
2. 回滚应用代码到 `/tmp/backend-before-p2.sha` 中记录的提交；
3. 若新迁移已经写入真实消费流水，优先修复前滚，不要随意执行数据库反向迁移；
4. 只有在确认 P2 上线后没有产生新订单、退款、消费调整时，才可恢复上线前数据库备份；
5. 恢复 `media`、环境变量、Nginx 和支付证书后重新运行 `python manage.py check`。

## 8. 服务器迁移到新主机

迁移顺序：

1. 新主机安装相同大版本的 Python、PostgreSQL、Nginx；
2. 恢复数据库和 `media`；
3. 放置 `.env` 与微信支付证书，权限设为仅服务账号可读；
4. 更新域名解析前，先通过 hosts 或临时域名验证健康检查；
5. 验证支付回调、虚拟支付、音频和图片访问；
6. 降低 DNS TTL 后切流；
7. 旧主机保留只读状态至少 48 小时，再销毁敏感数据。
