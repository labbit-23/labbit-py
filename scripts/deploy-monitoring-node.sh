#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   NODE_ROLE=local /opt/labbit-py/scripts/deploy-monitoring-node.sh
#   NODE_ROLE=vps /opt/labbit-py/scripts/deploy-monitoring-node.sh
# Optional overrides:
#   APP_DIR=/opt/labbit-py
#   BRANCH=main
#   PM2_APP_NAME=labbit-monitoring-local|labbit-monitoring-vps

APP_DIR="${APP_DIR:-/opt/labbit-py}"
BRANCH="${BRANCH:-main}"
NODE_ROLE="${NODE_ROLE:-}"

if [ -z "${NODE_ROLE}" ]; then
  echo "❌ NODE_ROLE is required (local|vps)"
  exit 1
fi

case "${NODE_ROLE}" in
  local)
    DEFAULT_PM2_APP="labbit-monitoring-local"
    ;;
  vps)
    DEFAULT_PM2_APP="labbit-monitoring-vps"
    ;;
  *)
    echo "❌ Invalid NODE_ROLE='${NODE_ROLE}'. Use local or vps"
    exit 1
    ;;
esac

PM2_APP_NAME="${PM2_APP_NAME:-$DEFAULT_PM2_APP}"

echo "==> Deploying monitoring node (${NODE_ROLE}) from ${APP_DIR} (${BRANCH})"
cd "${APP_DIR}"

if [ ! -d .git ]; then
  echo "❌ ${APP_DIR} is not a git repository"
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "❌ Working tree is dirty. Commit/stash/revert before deploy."
  git status --short
  exit 1
fi

echo "==> Pull latest"
git fetch origin "${BRANCH}"
git checkout "${BRANCH}"
git pull --ff-only origin "${BRANCH}"

echo "==> Ensure venv deps"
source "${APP_DIR}/venv/bin/activate"
pip install -r "${APP_DIR}/requirements.txt"

echo "==> Restart PM2 monitoring app (${PM2_APP_NAME})"
if pm2 describe "${PM2_APP_NAME}" >/dev/null 2>&1; then
  pm2 restart "${PM2_APP_NAME}" --update-env
else
  pm2 start "${APP_DIR}/ecosystem.monitoring.config.cjs" --only "${PM2_APP_NAME}" --update-env
fi

pm2 save
pm2 ls

echo "✅ Monitoring deploy complete (${NODE_ROLE})"
