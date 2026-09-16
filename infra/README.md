# Deploying to Hetzner

## Production server

| | |
|---|---|
| Server | `talento-api-1`, Hetzner CX23 (2 vCPU, 4 GB), Nuremberg, Ubuntu 26.04 |
| IPv4 / IPv6 | `91.98.226.51` / `2a01:4f8:c0c:77a7::1` |
| Firewall | `firewall-1`: inbound TCP 22, 80, 443 and ICMP |
| DNS | `api.talentoafrica.com` A record in Cloudflare, **DNS only** (Caddy issues the certificate) |
| Access | `ssh talento-api` (user `deploy`, key `~/.ssh/talento_hetzner`); root with the same key for apt |
| App dir | `/opt/talento` (`.env`, compose file, Caddyfile, `src/` checkout) |

First boot ran a cloud-init script that installed Docker, fail2ban and unattended upgrades, created
the `deploy` user, added 2 GB swap and disabled SSH password login. `docker-buildx` was installed
afterwards so the Dockerfile's BuildKit cache mounts work.

## Deploy

```bash
scripts/deploy.sh
```

Syncs the compose file and Caddyfile, pulls `main` into `/opt/talento/src`, builds `talento-api:local`
on the server, restarts the stack and waits for `https://api.talentoafrica.com/readyz`.

Secrets live only in `/opt/talento/.env` on the server (mode 600). Edit them there, then rerun the deploy.

CI also pushes `ghcr.io/ferris-hq/talento-api` on every push to `main`. To pull that instead of
building on the server, make the package public or `docker login ghcr.io` on the server, and set
`API_IMAGE` in the server `.env`.

## Database migrations

```bash
supabase link --project-ref <ref>
supabase db push
```

Run against the **staging** project first. Migrations are forward-only; write a new migration to undo a change.
