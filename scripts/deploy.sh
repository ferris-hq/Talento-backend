#!/usr/bin/env bash
# Deploys the current `main` to the Hetzner API server by building the image on the server.
#
#   scripts/deploy.sh            # uses the `talento-api` host from ~/.ssh/config
#   DEPLOY_HOST=deploy@1.2.3.4 scripts/deploy.sh
#
# The server keeps its secrets in /opt/talento/.env (not in git).
set -euo pipefail

HOST="${DEPLOY_HOST:-talento-api}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> sync compose + Caddy config"
scp -q "$ROOT/infra/docker-compose.prod.yml" "$ROOT/infra/Caddyfile" "$HOST:/opt/talento/"

echo "==> build and restart on $HOST"
ssh "$HOST" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/talento
if [ -d src/.git ]; then
  git -C src fetch -q origin main && git -C src reset -q --hard origin/main
else
  git clone -q https://github.com/ferris-hq/Talento-backend.git src
fi
echo "    commit $(git -C src rev-parse --short HEAD)"
DOCKER_BUILDKIT=1 docker build -q -t talento-api:local src/backend >/dev/null
docker compose -f docker-compose.prod.yml --env-file .env up -d --remove-orphans
docker image prune -f >/dev/null
REMOTE

echo "==> health check"
domain="$(ssh "$HOST" "grep '^API_DOMAIN=' /opt/talento/.env | cut -d= -f2")"
for attempt in $(seq 1 20); do
  if curl -fsS -m 5 "https://$domain/readyz"; then
    echo
    echo "==> deployed to https://$domain"
    exit 0
  fi
  sleep 3
done
echo "==> /readyz did not become healthy" >&2
exit 1
