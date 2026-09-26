#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
PAYMENT_EXPIRY_CRON_INTERVAL="${PAYMENT_EXPIRY_CRON_INTERVAL:-* * * * *}"
WALLET_MAINTENANCE_CRON_INTERVAL="${WALLET_MAINTENANCE_CRON_INTERVAL:-* * * * *}"
EARNINGS_RELEASE_CRON_INTERVAL="${EARNINGS_RELEASE_CRON_INTERVAL:-0 * * * *}"
PAYMENT_EXPIRY_LOG_DIR="${PAYMENT_EXPIRY_LOG_DIR:-$PROJECT_ROOT/logs}"

CRON_BEGIN="# BEGIN miniprogram-backend order-expiry"
CRON_END="# END miniprogram-backend order-expiry"
LEGACY_CRON_BEGIN="# BEGIN miniprogram-backend unpaid-order-expiry"
LEGACY_CRON_END="# END miniprogram-backend unpaid-order-expiry"

if ! command -v crontab >/dev/null 2>&1; then
  echo "未找到 crontab，无法安装订单和钱包维护任务。" >&2
  echo "请安装 cron/cronie，或设置 INSTALL_PAYMENT_EXPIRY_CRON=false 后自行使用 systemd timer 调用：" >&2
  echo "  $PYTHON_BIN $PROJECT_ROOT/manage.py expire_unpaid_orders" >&2
  echo "  $PYTHON_BIN $PROJECT_ROOT/manage.py expire_room_entries" >&2
  echo "  $PYTHON_BIN $PROJECT_ROOT/manage.py reconcile_recharges" >&2
  echo "  $PYTHON_BIN $PROJECT_ROOT/manage.py retry_recharge_deliveries" >&2
  echo "  $PYTHON_BIN $PROJECT_ROOT/manage.py release_earnings" >&2
  exit 1
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python 解释器不存在或不可执行：$PYTHON_BIN" >&2
  exit 1
fi

mkdir -p "$PAYMENT_EXPIRY_LOG_DIR"

project_root_quoted="$(printf '%q' "$PROJECT_ROOT")"
python_bin_quoted="$(printf '%q' "$PYTHON_BIN")"

cron_command() {
  local command_name="$1"
  local log_path="$PAYMENT_EXPIRY_LOG_DIR/${command_name}.log"
  local lock_path="$PAYMENT_EXPIRY_LOG_DIR/${command_name}.lock"
  local base="cd $project_root_quoted && $python_bin_quoted manage.py $command_name >> $(printf '%q' "$log_path") 2>&1"
  if command -v flock >/dev/null 2>&1; then
    printf 'flock -n %q bash -lc %q' "$lock_path" "$base"
  else
    printf '%s' "$base"
  fi
}

unpaid_command="$(cron_command expire_unpaid_orders)"
room_command="$(cron_command expire_room_entries)"
reconcile_command="$(cron_command reconcile_recharges)"
delivery_command="$(cron_command retry_recharge_deliveries)"
earnings_command="$(cron_command release_earnings)"

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
  printf '%s %s\n' "$WALLET_MAINTENANCE_CRON_INTERVAL" "$reconcile_command"
  printf '%s %s\n' "$WALLET_MAINTENANCE_CRON_INTERVAL" "$delivery_command"
  printf '%s %s\n' "$EARNINGS_RELEASE_CRON_INTERVAL" "$earnings_command"
  printf '%s\n' "$CRON_END"
} | crontab -

cd "$PROJECT_ROOT"
"$PYTHON_BIN" manage.py expire_unpaid_orders
"$PYTHON_BIN" manage.py expire_room_entries
"$PYTHON_BIN" manage.py reconcile_recharges
"$PYTHON_BIN" manage.py retry_recharge_deliveries
"$PYTHON_BIN" manage.py release_earnings

printf '已安装订单超时任务：%s\n' "$PAYMENT_EXPIRY_CRON_INTERVAL"
printf '已安装钱包维护任务：%s\n' "$WALLET_MAINTENANCE_CRON_INTERVAL"
printf '已安装收益释放任务：%s\n' "$EARNINGS_RELEASE_CRON_INTERVAL"
printf '日志目录：%s\n' "$PAYMENT_EXPIRY_LOG_DIR"
