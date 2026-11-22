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

BRANCH="${1:-main}"
COMPOSE_DIR="${COMPOSE_DIR:-/opt/chiefvoice}"
VITE_OIDC_AUTHORITY="${VITE_OIDC_AUTHORITY:-https://dev-ls5gkmim0q0ioeu1.us.auth0.com/}"
VITE_OIDC_CLIENT_ID="${VITE_OIDC_CLIENT_ID:-JjVV1d1Rn69oazE7h7s5JOuwbvAA6q4K}"
VITE_OIDC_AUDIENCE="${VITE_OIDC_AUDIENCE:-https://core.elvoryx.in/api}"
VITE_OIDC_SCOPE="${VITE_OIDC_SCOPE:-openid profile email platform:customers:read platform:customers:write}"
HEALTHCHECK_URL="${HEALTHCHECK_URL:-https://core.elvoryx.in/healthz}"

if [ ! -f "${COMPOSE_DIR}/compose.yaml" ]; then
  echo "Error: Compose file '${COMPOSE_DIR}/compose.yaml' not found" >&2
  exit 1
fi

# Coordinate with Cloud Build deployments, which use the same VM lock.
exec 9>/var/lock/chiefvoice-deploy.lock
flock 9

echo "=========================================="
echo " Starting Chiefvoice Deployment"
echo " Repo directory:    ${REPO_DIR}"
echo " Git branch:        ${BRANCH}"
echo " Compose directory: ${COMPOSE_DIR}"
echo "=========================================="

cd "${REPO_DIR}"

echo "==> 1/5 Pulling latest code from GitHub (${BRANCH})..."
git remote get-url origin >/dev/null
git fetch origin "${BRANCH}:refs/remotes/origin/${BRANCH}"
if git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  git checkout "${BRANCH}"
else
  git checkout --track -b "${BRANCH}" "origin/${BRANCH}"
fi
git merge --ff-only "origin/${BRANCH}"
if [ "$(git rev-parse HEAD)" != "$(git rev-parse "origin/${BRANCH}")" ]; then
  echo "Error: local '${BRANCH}' has commits not present on origin; refusing to deploy it" >&2
  exit 1
fi
echo "Deploying commit $(git rev-parse --short HEAD)"

echo "==> 2/5 Building backend API image (chiefvoice-api:local)..."
docker build -t chiefvoice-api:local .

echo "==> 3/5 Building frontend Console image (chiefvoice-console:local)..."
docker build -f frontend/Dockerfile.production \
  --build-arg VITE_OIDC_AUTHORITY="${VITE_OIDC_AUTHORITY}" \
  --build-arg VITE_OIDC_CLIENT_ID="${VITE_OIDC_CLIENT_ID}" \
  --build-arg VITE_OIDC_AUDIENCE="${VITE_OIDC_AUDIENCE}" \
  --build-arg VITE_OIDC_SCOPE="${VITE_OIDC_SCOPE}" \
  -t chiefvoice-console:local frontend

echo "==> 4/5 Running database migrations..."
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
if curl -fsS --max-time 10 "${HEALTHCHECK_URL}" >/dev/null 2>&1; then
  echo "✓ ${HEALTHCHECK_URL} is healthy."
else
  echo "! Health check failed or is unreachable: ${HEALTHCHECK_URL}" >&2
  exit 1
fi
