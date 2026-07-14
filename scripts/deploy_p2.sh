#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
BRANCH="${BRANCH:-feat/wechat-pay-v3}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
PIP_BIN="${PIP_BIN:-$PROJECT_ROOT/.venv/bin/pip}"
SERVICE_NAME="${SERVICE_NAME:-}"

cd "$PROJECT_ROOT"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "工作区存在未提交改动，停止部署：" >&2
  git status --short >&2
  exit 2
fi

current_branch="$(git branch --show-current)"
if [[ "$current_branch" != "$BRANCH" ]]; then
  git checkout "$BRANCH"
fi

git fetch origin "$BRANCH"
git pull --ff-only origin "$BRANCH"

"$PIP_BIN" install -r requirements.txt
"$PYTHON_BIN" manage.py check
"$PYTHON_BIN" manage.py migrate --plan
"$PYTHON_BIN" manage.py migrate
"$PYTHON_BIN" manage.py collectstatic --noinput
"$PYTHON_BIN" manage.py bootstrap_admin_roles --clear
"$PYTHON_BIN" manage.py backfill_boss_consumption
"$PYTHON_BIN" manage.py p2_healthcheck

if [[ -n "$SERVICE_NAME" ]]; then
  sudo systemctl restart "$SERVICE_NAME"
  sudo systemctl is-active --quiet "$SERVICE_NAME"
  echo "服务已重启：$SERVICE_NAME"
else
  echo "代码、迁移和健康检查已完成。未设置 SERVICE_NAME，请手动重启实际 Django/Gunicorn 服务。"
fi
