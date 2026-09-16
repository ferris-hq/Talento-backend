# Deploying to Hetzner

## One-time server setup (API server)

1. Create a server in **Nuremberg** (shared vCPU, 4 vCPU / 8 GB is plenty to start), Ubuntu 24.04, with your SSH key.
2. Attach a **Hetzner Cloud Firewall**: allow TCP 80, 443 and UDP 443 from anywhere; TCP 22 only from your IP.
3. In Cloudflare DNS for `talentoafrica.com`, add A/AAAA records `api` → the server's IPv4/IPv6.
   Keep them **DNS only** (grey cloud) so Caddy can get its own certificate, or proxy them and set
   SSL/TLS mode to **Full (strict)**.
4. On the server:

   ```bash
   apt-get update && apt-get install -y docker.io docker-compose-v2
   adduser --disabled-password deploy && usermod -aG docker deploy
   mkdir -p /opt/talento && chown deploy:deploy /opt/talento
   ```

5. Copy `docker-compose.prod.yml` and `Caddyfile` into `/opt/talento/`, and create `/opt/talento/.env`
   from `../.env.example` with production values (`chmod 600`).
6. Log in to GHCR once as `deploy` with a read-only token:
   `echo <token> | docker login ghcr.io -u <github-user> --password-stdin`

## Deploy

```bash
cd /opt/talento
docker compose -f docker-compose.prod.yml --env-file .env pull
docker compose -f docker-compose.prod.yml --env-file .env up -d
curl -fsS https://$API_DOMAIN/readyz
```

CI builds and pushes `ghcr.io/ferris-hq/talento-api` on every push to `main`. A deploy workflow that runs
the commands above over SSH gets added once the server exists.

## Database migrations

```bash
supabase link --project-ref <ref>
supabase db push
```

Run against the **staging** project first. Migrations are forward-only; write a new migration to undo a change.
