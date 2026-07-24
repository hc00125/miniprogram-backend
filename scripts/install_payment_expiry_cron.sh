#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
PAYMENT_EXPIRY_CRON_INTERVAL="${PAYMENT_EXPIRY_CRON_INTERVAL:-* * * * *}"
PAYMENT_EXPIRY_LOG_DIR="${PAYMENT_EXPIRY_LOG_DIR:-$PROJECT_ROOT/logs}"

CRON_BEGIN="# BEGIN miniprogram-backend order-expiry"
CRON_END="# END miniprogram-backend order-expiry"
LEGACY_CRON_BEGIN="# BEGIN miniprogram-backend unpaid-order-expiry"
LEGACY_CRON_END="# END miniprogram-backend unpaid-order-expiry"

if ! command -v crontab >/dev/null 2>&1; then
  echo "未找到 crontab，无法安装订单超时任务。" >&2
  echo "请安装 cron/cronie，或设置 INSTALL_PAYMENT_EXPIRY_CRON=false 后自行使用 systemd timer 每分钟调用：" >&2
  echo "  $PYTHON_BIN $PROJECT_ROOT/manage.py expire_unpaid_orders" >&2
  echo "  $PYTHON_BIN $PROJECT_ROOT/manage.py expire_room_entries" >&2
  exit 1
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python 解释器不存在或不可执行：$PYTHON_BIN" >&2
  exit 1
fi

mkdir -p "$PAYMENT_EXPIRY_LOG_DIR"

project_root_quoted="$(printf '%q' "$PROJECT_ROOT")"
python_bin_quoted="$(printf '%q' "$PYTHON_BIN")"
unpaid_log_quoted="$(printf '%q' "$PAYMENT_EXPIRY_LOG_DIR/expire_unpaid_orders.log")"
room_log_quoted="$(printf '%q' "$PAYMENT_EXPIRY_LOG_DIR/expire_room_entries.log")"
unpaid_command="cd $project_root_quoted && $python_bin_quoted manage.py expire_unpaid_orders >> $unpaid_log_quoted 2>&1"
room_command="cd $project_root_quoted && $python_bin_quoted manage.py expire_room_entries >> $room_log_quoted 2>&1"

existing_crontab="$(crontab -l 2>/dev/null || true)"
cleaned_crontab="$(
  printf '%s\n' "$existing_crontab" | awk \
    -v begin="$CRON_BEGIN" \
    -v end="$CRON_END" \
    -v legacy_begin="$LEGACY_CRON_BEGIN" \
    -v legacy_end="$LEGACY_CRON_END" '
      $0 == begin || $0 == legacy_begin { skipping = 1; next }
      $0 == end || $0 == legacy_end { skipping = 0; next }
      !skipping { print }
    '
)"

{
  printf '%s\n' "$cleaned_crontab"
  printf '%s\n' "$CRON_BEGIN"
  printf '%s %s\n' "$PAYMENT_EXPIRY_CRON_INTERVAL" "$unpaid_command"
  printf '%s %s\n' "$PAYMENT_EXPIRY_CRON_INTERVAL" "$room_command"
  printf '%s\n' "$CRON_END"
} | crontab -

cd "$PROJECT_ROOT"
"$PYTHON_BIN" manage.py expire_unpaid_orders
"$PYTHON_BIN" manage.py expire_room_entries

echo "已安装订单超时任务：$PAYMENT_EXPIRY_CRON_INTERVAL"
echo "未支付订单任务：$unpaid_command"
echo "进入房间补位任务：$room_command"
echo "日志目录：$PAYMENT_EXPIRY_LOG_DIR"
