#!/bin/bash
set -euo pipefail

# Elevate to root automatically if needed
if [ "${EUID}" -ne 0 ]; then
  exec sudo -E bash "$0" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/../compose.yaml" ] || [ -d "${SCRIPT_DIR}/../src" ]; then
  REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  REPO_DIR="${SCRIPT_DIR}"
fi

BRANCH="${1:-master}"
COMPOSE_DIR="${COMPOSE_DIR:-/opt/chiefvoice}"

echo "=========================================="
echo " Starting Chiefvoice Deployment"
echo " Repo directory:    ${REPO_DIR}"
echo " Git branch:        ${BRANCH}"
echo " Compose directory: ${COMPOSE_DIR}"
echo "=========================================="

cd "${REPO_DIR}"

echo "==> 1/5 Pulling latest code from GitHub (${BRANCH})..."
git checkout "${BRANCH}"
git pull origin "${BRANCH}"

echo "==> 2/5 Building backend API image (chiefvoice-api:local)..."
docker build -t chiefvoice-api:local .

echo "==> 3/5 Building frontend Console image (chiefvoice-console:local)..."
docker build -f frontend/Dockerfile.production \
  --build-arg VITE_OIDC_AUTHORITY="https://dev-ls5gkmim0q0ioeu1.us.auth0.com/" \
  --build-arg VITE_OIDC_CLIENT_ID="JjVV1d1Rn69oazE7h7s5JOuwbvAA6q4K" \
  --build-arg VITE_OIDC_AUDIENCE="https://chiefvoice.elvoryx.in/api" \
  --build-arg VITE_OIDC_SCOPE="openid profile email platform:customers:read platform:customers:write" \
  -t chiefvoice-console:local frontend

echo "==> 4/5 Running database migrations..."
if [ ! -d "${COMPOSE_DIR}" ] || [ ! -f "${COMPOSE_DIR}/compose.yaml" ]; then
  echo "Error: Compose directory '${COMPOSE_DIR}' not found or missing compose.yaml" >&2
  exit 1
fi

API_IMAGE="chiefvoice-api:local" FRONTEND_IMAGE="chiefvoice-console:local" \
  docker compose -f "${COMPOSE_DIR}/compose.yaml" --project-directory "${COMPOSE_DIR}" \
  run --rm --no-deps api alembic upgrade head

echo "==> 5/5 Restarting containers with updated images..."
API_IMAGE="chiefvoice-api:local" FRONTEND_IMAGE="chiefvoice-console:local" \
  docker compose -f "${COMPOSE_DIR}/compose.yaml" --project-directory "${COMPOSE_DIR}" \
  up -d --no-build api frontend caddy

echo ""
echo "=========================================="
echo " Deployment Complete!"
echo " Current Container Status:"
echo "=========================================="
docker compose -f "${COMPOSE_DIR}/compose.yaml" --project-directory "${COMPOSE_DIR}" ps
echo ""
echo "Verifying health endpoint..."
if curl -sS --max-time 10 https://chiefvoice.elvoryx.in/healthz >/dev/null 2>&1; then
  echo "✓ https://chiefvoice.elvoryx.in/healthz is healthy."
else
  echo "! Public endpoint check timed out or unreachable from local network."
fi
