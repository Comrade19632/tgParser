# TG Parser — production runbook

This is the minimal ops guide for deploying and operating TG Parser on a Linux host.

## 0) Prereqs (host)

- Docker + Docker Compose v2
- Recommended: a dedicated VM, firewall, and backups

Quick check:

```bash
docker --version
docker compose version
```

## 1) Deploy (first time)

```bash
# 1) clone
cd /opt
git clone https://github.com/Comrade19632/tgParser.git
cd tgParser

# 2) config
cp .env.example .env
# Edit .env and set at least:
#   BOT_TOKEN=
#   POSTGRES_PASSWORD= (change from default)
#   DATABASE_URL=postgresql+psycopg://tgparser:<PASS>@db:5432/tgparser

# 3) start
# - base file defines services
# - prod overrides add logging/restart policies + stable volume names
sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# 4) verify
sudo docker compose ps
sudo docker compose logs -n 200 migrate
sudo docker compose logs -n 200 bot
sudo docker compose logs -n 200 worker
```

Expected:
- `migrate` exits with code 0
- `bot` starts and listens for Telegram updates
- `worker` runs and logs a tick once per hour (and uses Redis lock)

## 2) Update (rolling)

```bash
cd /opt/tgParser
git pull
sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Notes:
- `migrate` runs automatically on each start and applies Alembic migrations.

## 3) Logs

```bash
cd /opt/tgParser
sudo docker compose logs -f bot
sudo docker compose logs -f worker
```

Log rotation is configured in `docker-compose.prod.yml` (json-file driver max-size/max-file).

## 4) DB backup / restore

### Backup (pg_dump)

```bash
cd /opt/tgParser
# creates a plain SQL dump on the host
sudo docker compose exec -T db pg_dump -U tgparser -d tgparser > tgparser_$(date -u +%F_%H%M%S).sql
```

### Restore

```bash
cd /opt/tgParser
# WARNING: this overwrites data
cat tgparser_dump.sql | sudo docker compose exec -T db psql -U tgparser -d tgparser
```

If you prefer compressed dumps:

```bash
sudo docker compose exec -T db pg_dump -U tgparser -d tgparser | gzip -c > tgparser_$(date -u +%F_%H%M%S).sql.gz
```

## 5) Common failures

- **migrate fails / DB not ready**: check `db` healthcheck + credentials; then rerun `docker compose up -d`.
- **bot doesn’t respond**: verify `BOT_TOKEN` and container logs.
- **worker running twice**: ensure only one stack is deployed; Redis lock should prevent double ticks, but fix duplicate deployments.

## 6) Security notes (MVP)

- Do not expose Postgres/Redis ports publicly.
- Keep `.env` readable only by root/admin.
- Rotate BOT_TOKEN if it ever leaks.
