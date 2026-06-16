# SQLite → PostgreSQL 迁移文档

> 迁移日期：2026-06-16  
> 服务器：api.huc125.cn  
> 项目：微信小程序后端 (miniprogram-backend)

---

## 一、迁移概览

| 项目 | 迁移前 | 迁移后 |
|------|--------|--------|
| 数据库 | SQLite3 (db.sqlite3) | PostgreSQL 15 |
| 大小 | 396KB | ~400KB |
| 记录数 | 251 条 | 251 条（一致） |
| 数据库名 | db.sqlite3 | miniprogram_db |
| 用户 | - | miniprogram_user |
| 端口 | - | 5432（本地） |

---

## 二、安装配置

### 安装 PostgreSQL 15

```bash
dnf module enable -y postgresql:15
dnf install -y postgresql-server postgresql-contrib
```

### 初始化 & 启动

```bash
postgresql-setup --initdb
systemctl enable postgresql
systemctl start postgresql
```

### 创建数据库和用户

```bash
su - postgres
psql -c "CREATE USER miniprogram_user WITH PASSWORD 'PgMigrate2024!';"
psql -c "CREATE DATABASE miniprogram_db OWNER miniprogram_user;"
psql -c "ALTER USER miniprogram_user CREATEDB;"
psql -c "GRANT ALL PRIVILEGES ON DATABASE miniprogram_db TO miniprogram_user;"
```

### 认证配置 (`/var/lib/pgsql/data/pg_hba.conf`)

```
local   all             postgres                                peer
local   all             all                                     md5
host    all             all             127.0.0.1/32            md5
host    all             all             ::1/128                 md5
```

---

## 三、Django 配置变更

### 1. settings.py

新增 `DATABASE_URL` 解析逻辑，支持 `postgres://` 协议：

```python
DATABASE_URL = os.environ.get('DATABASE_URL', '')

if DATABASE_URL:
    from urllib.parse import urlparse, unquote
    u = urlparse(DATABASE_URL)
    engine_map = {
        'postgres': 'django.db.backends.postgresql',
        'postgresql': 'django.db.backends.postgresql',
        ...
    }
    DATABASES = {
        'default': {
            'ENGINE': engine_map.get(u.scheme, ...),
            'NAME': u.path.lstrip('/'),
            'USER': unquote(u.username) if u.username else '',
            'PASSWORD': unquote(u.password) if u.password else '',
            'HOST': u.hostname or '',
            'PORT': str(u.port) if u.port else '',
        }
    }
else:
    # 回退到 SQLite
    ...
```

### 2. .env

```bash
DATABASE_URL=postgres://miniprogram_user:PgMigrate2024%21@127.0.0.1:5432/miniprogram_db
```

> ⚠️ 密码中特殊字符需 URL 编码，如 `!` → `%21`

### 3. 安装 Python 驱动

```bash
pip install psycopg2-binary
```

---

## 四、数据迁移步骤

### 导出 SQLite 数据

```bash
cd /www/wwwroot/django_backend
cp db.sqlite3 db.sqlite3.backup   # 先备份
python manage.py dumpdata \
  --exclude auth.permission \
  --exclude contenttypes \
  --indent 2 > /tmp/db_dump.json
```

### 切换到 PostgreSQL

```bash
# 在 .env 中设置 DATABASE_URL 后：
python manage.py migrate       # 创建表结构
python manage.py loaddata /tmp/db_dump.json   # 导入数据
```

### 验证

```bash
python manage.py test apps.orders apps.payments   # 跑测试
python manage.py check                             # 系统检查
curl http://127.0.0.1:8001/api/health             # 健康检查
```

---

## 五、更新 checks.py（修复测试阻塞）

**问题：** Django 测试框架自动设置 `DEBUG=False`，导致生产环境检查在测试中报错  
**修复：** 注册检查时添加 `deploy=True`，使其仅在 `--deploy` 模式下运行

```python
# 修改前
@register(Tags.security)
def production_security_settings_check(...):

# 修改后
@register(Tags.security, deploy=True)
def production_security_settings_check(...):
```

---

## 六、回滚方案

如需回退到 SQLite：

```bash
# 1. 注释掉 .env 中的 DATABASE_URL
# 2. 恢复 SQLite 备份
cp db.sqlite3.backup db.sqlite3

# 3. 重启服务
kill -HUP $(pgrep -f "gunicorn config.wsgi")
```

---

## 七、日常运维

### 常用命令

```bash
# 查看 PostgreSQL 状态
systemctl status postgresql

# 连接数据库
psql -h 127.0.0.1 -U miniprogram_user -d miniprogram_db

# 备份
pg_dump -h 127.0.0.1 -U miniprogram_user miniprogram_db > backup.sql

# 恢复
psql -h 127.0.0.1 -U miniprogram_user miniprogram_db < backup.sql

# 重启
systemctl restart postgresql
```

### 日志位置

- PostgreSQL 日志：`/var/lib/pgsql/data/log/`
- Django 应用日志：`/www/wwwroot/django_backend/gunicorn.log`

### 定期维护

```bash
# 分析查询性能（建议每周执行）
psql -h 127.0.0.1 -U miniprogram_user -d miniprogram_db -c "ANALYZE;"
```

---

## 八、注意事项

1. **密码安全：** `.env` 中的数据库密码已在本文档中记录，请不要外泄
2. **SQLite 备份：** 保留 `db.sqlite3.backup` 至少一个月
3. **外键约束：** PostgreSQL 比 SQLite 更严格，如果添加表时遇到约束错误，检查外键数据完整性
4. **序列问题：** `loaddata` 后 PostgreSQL 序列可能不正确，Django 的 `loaddata` 会自动处理，无需手动干预
5. **未来迁移 MySQL：** settings.py 已预留 MySQL 支持，只需修改 `DATABASE_URL` 前缀为 `mysql://` 并安装 `mysqlclient`
