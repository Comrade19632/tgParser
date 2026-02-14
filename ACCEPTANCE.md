# TG Parser — Release DoD (E2E acceptance) checklist

This checklist is used to validate the v1 release end-to-end.

## Prereqs

- Deployed stack is up:

```bash
cd /opt/tgParser
sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

- Logs are clean-ish (no crash loops):

```bash
sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -n 200 bot
sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -n 200 worker
```

## 1) Add 2 accounts (both ways)

### 1.1 Phone-code login (public auth)

In the Telegram bot:
- Menu → **Accounts** → **Add** → **Phone-code**
- Provide phone in international format, confirm code

Expected:
- account appears in **Accounts list**
- account status becomes `active`

### 1.2 tdata import

In the Telegram bot:
- Menu → **Accounts** → **Add** → **tdata**
- Upload/import tdata payload (per bot prompt)

Expected:
- account appears in **Accounts list**
- status becomes `active`

## 2) Add channels (public + private)

### 2.1 Public channel

- Menu → **Channels** → **Add**
- Provide @username or t.me link

Expected:
- channel appears in list as `active`

### 2.2 Private channel

- Menu → **Channels** → **Add**
- Provide invite link

Expected:
- bot reports join request created or membership confirmed
- channel appears with `pending_approval` (if admin approval required)

## 3) Pending approval scenario

If join requires admin approval:
- approve join request from a channel admin account

Expected:
- channel status changes from `pending_approval` → `active`

## 4) Hourly tick persists posts

There are two ways:

### 4.1 Wait for scheduled worker tick

Expected (worker logs):
- `tick: ok ... posts_inserted=<n>`

### 4.2 Trigger a single tick manually (recommended for acceptance)

```bash
cd /opt/tgParser
sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T worker python -m tgparser.tick_once --force
```

Expected:
- command exits 0
- worker logs show a successful tick

## 5) FloodWait does not crash the run

Hard to force deterministically, but verify resilience logic is enabled by inspection:

- worker logs contain warnings (not crashes) when FloodWait happens
- accounts are cooled down/quarantined rather than hard failing the whole tick

## Evidence

Record in the Dev task comment:
- the exact channels used (redact if private)
- timestamps of successful bot actions
- relevant worker log lines (no secrets)
- if any step fails: error message + what was attempted
