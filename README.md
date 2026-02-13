# tgParser

Telegram channels text parser.

- Control plane: Telegram bot (no web UI)
- Parsing: Telethon userbots
- Storage: PostgreSQL
- Scheduling: hourly worker loop with Redis lock

## Quick start (dev)

```bash
cp .env.example .env
# fill BOT_TOKEN, etc.

docker compose up --build
```

Services:
- `bot`: aiogram bot, menus/commands
- `worker`: hourly parser loop (stubbed initially)
- `db`: Postgres
- `redis`: lock/state

## Notes
- This repo is intentionally scaffold-first. Implementations for account onboarding flows (phone code / tdata) are referenced from `tgreact` but not vendored here.
