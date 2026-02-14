# TG Parser — HTTP API (v1)

This project exposes a small HTTP API for integrations (export posts, manage channels).

## Base URL

Prod compose publishes the API directly on the host (binds to **0.0.0.0**):

- `http://<server-ip>:${TG_PARSER_API_PORT:-18081}`

No SSL / no domain by design. Access control is **bearer token** (see below).

## Auth

All endpoints require a client token:

- Header: `Authorization: Bearer <SERVICE_API_TOKEN>`

Token is configured in `.env`:

- `SERVICE_API_TOKEN=...`

If `SERVICE_API_TOKEN` is not set, API fails closed with `503`.

### Token rotation

1) Generate a new token (random 32+ chars).
2) Update `.env` (`SERVICE_API_TOKEN=...`).
3) Redeploy/restart api/bot/worker (`docker compose up -d --build`).
4) Update all clients to use the new token.

Note: tokens must never be sent as query params; use only the `Authorization` header.

## Endpoints

### GET /api/channels

Query params:
- `is_active` (bool)
- `type` (`public|private`)
- `q` (search in `identifier`/`title`)
- `limit` (default 50, max 200)
- `offset` (default 0)

Response:
- `{ total, limit, offset, items: [...] }`

### POST /api/channels

Body:
```json
{ "type": "public", "identifier": "durov", "backfill_days": 7, "is_active": true }
```

Upsert by `(type,identifier)`.

### GET /api/posts

Query params:
- `channel_id` OR `channel_identifier` (and optional `channel_type`)
- `date_from` / `date_to` (inclusive, ISO8601; if no TZ provided, treated as UTC)
- `limit` (default 50, max 200)
- `offset`

Response:
- `{ total, limit, offset, items: [...] }` (ordered by `published_at` desc)

## Curl examples

Replace `$TOKEN`.

401 (no token):
```bash
curl -i http://<server-ip>:18081/api/channels
```

List channels:
```bash
curl -s \
  -H "Authorization: Bearer $TOKEN" \
  "http://<server-ip>:18081/api/channels?limit=20&offset=0" | jq
```

Add/update channel:
```bash
curl -s \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type":"public","identifier":"durov","backfill_days":7,"is_active":true}' \
  http://<server-ip>:18081/api/channels | jq
```

Export posts with date filters:
```bash
curl -s \
  -H "Authorization: Bearer $TOKEN" \
  "http://<server-ip>:18081/api/posts?channel_identifier=durov&channel_type=public&date_from=2026-01-01T00:00:00Z&date_to=2026-02-01T00:00:00Z&limit=50" | jq
```
