# KOOK 人工限流恢复（R2；不构成上线授权）

本命令只操作 outbox/gate/audit，不调用 KOOK/微信，不改订单、支付、财务，不开启任何开关。默认 dry-run。运行权限必须仅授予批准的运维账号；CLI 的 actor 是审计声明，不是新的认证机制。当前单主机、单 worker 约束不变；跨主机或绕过管理命令直接调用 process_once 的进程不受文件锁保护，禁止这样部署。

## 告警、核验依据

- 按 bot 监控 KookRateLimit：`blocked_until.year == 9999` 为人工 hold；其余是有限 cooldown。不因数字大小推断 Reset 的单位。
- 新建/延长 gate 会写 `KookAuditLog.action=rate.hold`，actor=bot，object_id=gate主键，request_id=global或bucket SHA256，result=RESET_UNVERIFIED/COOLDOWN，created_at 为实际记录时间。它不保存 Token、原始 headers/body、bucket 原文。历史 gate 可能没有记录，报告为 LEGACY_REASON_UNRECORDED/null，**不能伪造历史发生时间或原因**。
- 获授权只读查看对应 bot 的 gate key、主键和精确 blocked_until（带时区，包括微秒），并核对配置版本。dry-run JSON 报告 queued 数、最老事件年龄、原因/记录时间、候选/跳过数和其余最大 gate。接入现有监控/人工值守；此补丁没有部署告警服务。
- 验证依据保存在受控变更单：官方明确确认或获准真实平台样本、Reset 单位、平台当前窗口已结束的证据、Bot/频道归属/配置版本、授权人与复核人、worker 暂停证据。`--evidence CHANGE-123` 是这份记录的引用，不是把秘密/headers贴进命令行。代码只校验引用存在和格式，不能替代人工确认真实性。
- 未明确确认 Reset 单位时保持 unverified/manual hold。不要按数值大小猜秒/毫秒；只改单位、只删 gate 都不能安全恢复已经推至 year9999 的 queued。

## 操作流程（仅在另行批准后执行）

1. 停止**全部** KOOK worker 并禁止自动重启，确认无仍在网络中的请求。不要停止无关服务。使用当前受审版本；不要在恢复期间换 bot/config。
2. 确認所有 worker 与恢复命令共享相同 `KOOK_WORKER_LOCK_PATH` 和 OS 身份/权限；默认 `/run/lock/huc125-kook-worker.lock`。不要删除/换 inode 解锁。命令以 O_NOFOLLOW + 独占非阻塞 flock 持有到事务结束；worker已运行则拒绝。即使拿到锁，bot仍有 inflight delivery 或 stream lease 也拒绝；必须先独立处理崩溃租约/unknown，不可用恢复命令把它们变 queued。
3. 用已核验部署解释器/显式批准的 settings，先 dry-run。下面全是占位参数，**本轮未对生产执行**：

   ```text
   <python> manage.py recover_kook_rate_limit --settings=<approved-settings> \
     --bot <exact-bot> --gate <global-or-exact-hashed-bucket> \
     --expected-until <exact-aware-ISO-deadline> --config-version <verified-version> \
     --actor <operator-reference> --evidence <approved-change-reference>
   ```

   不传 --execute 就不会改任何数据库行（会获取同一文件锁）。bot必须等于当前配置；版本必须与核验版本一致；gate/deadline发生变化则拒绝，不自动跟随新窗口。
4. 检查输出并将 JSON 纳入变更单；仅在授权复核后，使用完全相同参数加：

   ```text
   --execute --workers-paused --confirm <exact-bot>/<exact-gate>
   ```

   `--workers-paused` 仅是操作人声明，不能替代停止进程；真实 flock 是第二道门禁。不要通过改锁路径、改测试开关绕过。
5. 同一 DB 事务中仅删除指定 gate，复核并重新调度**该 bot、queued、next_attempt_at 等于该 gate 原 deadline、最新且未应用 revision、非 unknown 流、无 lease、仍在有效期、当前绑定/consent/配置/灰度/业务 fresh 全通过**的 delivery。身份错配停放不由此恢复。其他状态（含 cancelled/unknown/superseded/failed/sent）完全不复活。
6. 时间设为 now 与**其他仍有效 gate 的最大期限**；不删/缩短其他 gate，也不缩短与选中期限无关的 backoff。被推 year9999 且仍合格的 queued 因此同步解除远期调度；如另一个 manual gate 仍存在则继续远期等待。无效候选保持原状，不重新发送旧事件。发送开关关闭/业务不允许时会被跳过；**不应为了恢复打开开关**。检查 skipped，接受失效事件不补发；以后若要处置仍有效的残留，需单独受审操作，不能直接 SQL 改时间。
7. `rate.recover` 及 `.scope/.window/.config` 四条审计以同一 request_id(SHA256)关联：执行人、原 gate 主键、精确 bot/key、原 deadline、配置版本、变更单、释放数量。审计失败会回滚删除和时间修改；保存命令 JSON 输出，其中还有跳过/剩余窗口等摘要（哈希不是签名）。
8. 复核 gate/队列/audit 后，在另行批准的原有白名单与小批量设置下恢复唯一 worker，观察新 rate.hold、unknown、最老队列年龄与业务结果。不保证 exactly-once。若再 hold，应停下重新核验，绝不自动循环解除。

## 边界

- 不支持自动重启、自动反复清 gate、恢复 unknown、迁移旧 bot stream 或代替真实平台核验。
- `fresh` 按当前订单回调纯读复核；生产配置/权限及真实 Bot Token 身份仍须外部核验。默认源码开关保持关闭。
- 本轮测试只有隔离 SQLite/临时 Unix-socket PG + Mock；不构成真实平台发送验收。
