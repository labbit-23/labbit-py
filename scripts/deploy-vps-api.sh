#!/usr/bin/env bash
set -euo pipefail

# Optional overrides:
#   APP_DIR=/opt/labbit-py
#   BRANCH=main
#   PM2_API_APP_NAME=labbit-api
#   PM2_MONITORING_APP_NAME=labbit-monitoring
#   SKIP_MONITORING=1
#   HEALTHCHECK_URL=http://127.0.0.1:8000/health

APP_DIR="${APP_DIR:-/opt/labbit-py}"
BRANCH="${BRANCH:-main}"
PM2_API_APP_NAME="${PM2_API_APP_NAME:-labbit-api}"
PM2_MONITORING_APP_NAME="${PM2_MONITORING_APP_NAME:-labbit-monitoring}"
SKIP_MONITORING="${SKIP_MONITORING:-1}"
HEALTHCHECK_URL="${HEALTHCHECK_URL:-http://127.0.0.1:8000/health}"
HEALTHCHECK_RETRIES="${HEALTHCHECK_RETRIES:-20}"
HEALTHCHECK_DELAY_SECONDS="${HEALTHCHECK_DELAY_SECONDS:-3}"

echo "==> Deploying labbit-py from ${APP_DIR} (${BRANCH})"
cd "${APP_DIR}"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "❌ ${APP_DIR} is not a git repository."
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "❌ Working tree is dirty. Commit/stash/revert before deploy."
  git status --short
  exit 1
fi

echo "==> Pull latest"
git fetch origin "${BRANCH}"
git checkout "${BRANCH}"
git pull --ff-only origin "${BRANCH}"

echo "==> Install Python deps"
source ./venv/bin/activate
pip install -r requirements.txt

echo "==> Start/restart PM2 API service (${PM2_API_APP_NAME})"
if pm2 describe "${PM2_API_APP_NAME}" >/dev/null 2>&1; then
  pm2 restart "${PM2_API_APP_NAME}" --update-env
else
  pm2 start "${APP_DIR}/start.sh" --name "${PM2_API_APP_NAME}" --interpreter /usr/bin/bash
fi

if [ "${SKIP_MONITORING}" = "1" ]; then
  echo "==> Skipping monitoring service restart (SKIP_MONITORING=1)"
else
  echo "==> Start/restart PM2 monitoring service (${PM2_MONITORING_APP_NAME})"
  if pm2 describe "${PM2_MONITORING_APP_NAME}" >/dev/null 2>&1; then
    pm2 restart "${PM2_MONITORING_APP_NAME}" --update-env
  else
    pm2 start "${APP_DIR}/scripts/start-monitoring.sh" --name "${PM2_MONITORING_APP_NAME}" --interpreter /usr/bin/bash
  fi
fi

pm2 save

echo "==> Health check: ${HEALTHCHECK_URL}"
ok=0
for attempt in $(seq 1 "${HEALTHCHECK_RETRIES}"); do
  if curl -fsS "${HEALTHCHECK_URL}" >/dev/null; then
    ok=1
    echo "✅ Health check passed (${attempt}/${HEALTHCHECK_RETRIES})"
    break
  fi
  echo "⏳ Waiting for API (${attempt}/${HEALTHCHECK_RETRIES})..."
  sleep "${HEALTHCHECK_DELAY_SECONDS}"
done

if [ "${ok}" -ne 1 ]; then
  echo "❌ Health check failed."
  echo "Run: pm2 logs ${PM2_API_APP_NAME} --lines 120"
  exit 1
fi

echo "✅ Deploy complete."
