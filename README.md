# Titan

Titan is a high-performance API platform by Antmind Ventures Private Limited.

- Product: Titan
- Company: Antmind Ventures Private Limited
- Website: https://antmind.ai
- Production Domain: https://titan.antmind.ai

Built with FastAPI, PostgreSQL (pgvector), Redis, ARQ workers, Nginx, Docker, and Alembic.

## Start Here (Fast Setup)

If you only read one section, read this one.

### 1) Prepare environment

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

- `SECRET_KEY`
- `DB_PASSWORD`
- `REDIS_PASSWORD`

### 2) First boot

```bash
make setup
```

This builds images, starts Postgres/Redis, runs migrations, and starts all services.

### 3) Verify

```bash
curl -sS http://localhost/api/v1/health
```

Swagger docs are available at `http://localhost/docs`.

## Production First Deploy (After `make setup`)

### Prerequisites

- Domain DNS A record points to server IP.
- Ports 80 and 443 are open.
- `.env` secrets are set.
- If using Cloudflare, set DNS record to `DNS only` (gray cloud) while issuing the first certificate.
- If using Cloudflare, set SSL/TLS mode to `Full` during issuance, then `Full (strict)` after cert is installed.

### Exact sequence

1. Bring stack up once:

```bash
make setup
```

2. Generate and install TLS certificate:

```bash
make ssl
```

3. Validate HTTPS:

```bash
curl -I https://titan.antmind.ai/api/v1/health
```

4. Continue normal operations:

```bash
make status
make logs
```

What `make ssl` does:

- Temporarily switches Nginx to HTTP bootstrap mode (`titan.conf`) on production.
- Runs Certbot webroot challenge.
- Obtains Let's Encrypt certificate.
- Switches `.env` to `NGINX_CONF=titan-prod.conf`.
- Restarts Nginx with TLS config.

For future releases, use:

```bash
make deploy
```

## Daily Commands

```bash
make up
make down
make restart
make status
make logs
make logs-app
make logs-worker
make logs-nginx
```

## Migrations

```bash
make migrate
make migration name=<description>
make downgrade
make migration-history
```

Current migration baseline enables PostgreSQL extensions:

- `vector`
- `uuid-ossp`
- `pg_trgm`
- `btree_gin`

## Discover Catalog Seeding

Run this after migrations (or whenever discover seed data changes).

Local (from `backend/`):

```bash
alembic upgrade head
python3 seed.py
```

Docker Compose:

```bash
docker compose exec app alembic upgrade head
docker compose exec app python seed.py
```

`seed.py` fully reseeds discover catalog tables: it removes existing discover rows and inserts the latest seed data.

## API Endpoints

- `GET /` service metadata
- `GET /health` redirects to API health endpoint
- `GET /api/v1/health` app + database + redis + system health

## Background Jobs

- Worker service: `arq-worker`
- Worker entrypoint: `arq app.workers.worker.WorkerSettings`
- Current task: `health_ping_task`

## Configuration (Important Vars)

| Variable | Required | Default |
|---|---:|---|
| `SECRET_KEY` | Yes | - |
| `DB_HOST` | No | `postgres` |
| `DB_PORT` | No | `5432` |
| `DB_USER` | No | `titan` |
| `DB_PASSWORD` | Yes | - |
| `DB_NAME` | No | `titan` |
| `REDIS_HOST` | No | `redis` |
| `REDIS_PORT` | No | `6379` |
| `REDIS_PASSWORD` | Yes | - |
| `REDIS_DB` | No | `0` |
| `ARQ_QUEUE_NAME` | No | `titan:queue` |
| `WORKERS_COUNT` | No | `0` (auto) |
| `RUN_MIGRATIONS` | No | `false` |
| `NGINX_CONF` | No | `titan.conf` |

## Architecture at a Glance

```text
Client -> Nginx -> FastAPI app
                   |-> PostgreSQL (pgvector)
                   |-> Redis
                   |-> ARQ queue -> ARQ worker
```

## Extending Titan

To add a new service module:

1. Create `app/services/<name>/router.py`.
2. Include router in `app/main.py`.
3. Add models under `app/services/<name>/models/`.
4. Import that models package in `alembic/env.py`.
5. Generate and run migration.

## Troubleshooting

### TLS command fails

- Confirm DNS points to server.
- Confirm port 80 is reachable from internet.
- Ensure Cloudflare proxy is temporarily disabled (`DNS only`) for first certificate issuance.
- Ensure `NGINX_CONF=titan.conf` before running `make ssl`.
- Re-run `make ssl`.

### Nginx exits with missing certificate

Error example:

`cannot load certificate \"/etc/letsencrypt/live/<domain>/fullchain.pem\"`

Cause:

- Nginx is using production TLS config before Let's Encrypt files exist.
- This can happen on first deploy if `NGINX_CONF=titan-prod.conf` is already set.

Fix:

1. Set `NGINX_CONF=titan.conf` in `.env`.
2. Run `make up` (or `docker compose up -d nginx`) and verify HTTP is reachable on port 80.
3. Run `make ssl`.
4. Confirm Nginx is healthy: `make status` and `make logs-nginx`.

### Health is degraded

- `make logs-app`
- `make status`
- Check DB/Redis passwords in `.env`.

### Worker not consuming jobs

- `make status`
- `make logs-worker`
- Confirm `ARQ_QUEUE_NAME` matches producer and worker.

## License

Titan is fully open-source for any type of use, including personal and commercial use, as stated by Antmind Ventures Private Limited.

To make this explicit for all ecosystems, add a root `LICENSE` file with your preferred legal text (for example, MIT or Apache-2.0).
