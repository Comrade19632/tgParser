from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from tgparser.models import Channel, ChannelType, Post

from .deps import get_db, require_token
from .schemas import ChannelOut, ChannelUpsertIn, ChannelsListResponse, PostOut, PostsListResponse

logger = logging.getLogger("tgparser.api")

app = FastAPI(title="tgParser HTTP API", version="v1")


# Very simple in-memory rate limiter.
# Goal: protect the public port from obvious abuse. Not meant for multi-instance.
_RATE_WINDOW_SECONDS = 60
_RATE_MAX_REQUESTS = 120  # ~2 rps average per IP
_rate_state: dict[str, tuple[int, int]] = {}  # ip -> (window_start_ts, count)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    ip = (request.client.host if request.client else "unknown")
    now = int(time.time())
    window_start, count = _rate_state.get(ip, (now, 0))

    if now - window_start >= _RATE_WINDOW_SECONDS:
        window_start, count = now, 0

    count += 1
    _rate_state[ip] = (window_start, count)

    if count > _RATE_MAX_REQUESTS:
        retry_after = max(1, _RATE_WINDOW_SECONDS - (now - window_start))
        # Never log auth headers/tokens.
        logger.warning("rate_limited ip=%s path=%s", ip, request.url.path)
        return Response(
            status_code=429,
            content="rate_limited",
            headers={"Retry-After": str(retry_after)},
            media_type="text/plain",
        )

    return await call_next(request)


def _parse_dt(v: str | None, *, field: str) -> datetime | None:
    if v is None or v == "":
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail={field: "invalid_iso8601"}) from e

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@app.get("/health", dependencies=[Depends(require_token)])
def health():
    return {"ok": True}


@app.get("/api/channels", response_model=ChannelsListResponse, dependencies=[Depends(require_token)])
def list_channels(
    db: Session = Depends(get_db),
    is_active: bool | None = None,
    type: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    stmt = select(Channel)

    if is_active is not None:
        stmt = stmt.where(Channel.is_active == is_active)

    if type is not None and type != "":
        try:
            ctype = ChannelType(type)
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"type": "invalid"}) from e
        stmt = stmt.where(Channel.type == ctype)

    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Channel.identifier.ilike(like), Channel.title.ilike(like)))

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()

    items = db.execute(stmt.order_by(Channel.id.asc()).limit(limit).offset(offset)).scalars().all()

    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "items": [
            ChannelOut(
                id=c.id,
                type=c.type.value,
                identifier=c.identifier,
                title=c.title,
                is_active=c.is_active,
                access_status=c.access_status.value,
                backfill_days=c.backfill_days,
                peer_id=c.peer_id,
                last_checked_at=c.last_checked_at,
                last_error=c.last_error,
            )
            for c in items
        ],
    }


@app.post("/api/channels", response_model=ChannelOut, dependencies=[Depends(require_token)])
def upsert_channel(payload: ChannelUpsertIn, db: Session = Depends(get_db)):
    ctype = ChannelType(payload.type)

    existing = db.execute(
        select(Channel).where(Channel.type == ctype, Channel.identifier == payload.identifier)
    ).scalar_one_or_none()

    if existing is None:
        ch = Channel(
            type=ctype,
            identifier=payload.identifier,
            backfill_days=payload.backfill_days,
            is_active=payload.is_active,
        )
        db.add(ch)
        db.commit()
        db.refresh(ch)
    else:
        existing.backfill_days = payload.backfill_days
        existing.is_active = payload.is_active
        db.add(existing)
        db.commit()
        db.refresh(existing)
        ch = existing

    return ChannelOut(
        id=ch.id,
        type=ch.type.value,
        identifier=ch.identifier,
        title=ch.title,
        is_active=ch.is_active,
        access_status=ch.access_status.value,
        backfill_days=ch.backfill_days,
        peer_id=ch.peer_id,
        last_checked_at=ch.last_checked_at,
        last_error=ch.last_error,
    )


@app.get("/api/posts", response_model=PostsListResponse, dependencies=[Depends(require_token)])
def list_posts(
    db: Session = Depends(get_db),
    channel_id: int | None = None,
    channel_identifier: str | None = None,
    channel_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    dt_from = _parse_dt(date_from, field="date_from")
    dt_to = _parse_dt(date_to, field="date_to")

    # Channel filter is optional. If omitted, export posts across all channels.
    channel: Channel | None = None
    if channel_id is not None or channel_identifier:
        ch_stmt = select(Channel)
        if channel_id is not None:
            ch_stmt = ch_stmt.where(Channel.id == channel_id)
        else:
            ch_stmt = ch_stmt.where(Channel.identifier == channel_identifier)
            if channel_type:
                try:
                    ctype = ChannelType(channel_type)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail={"channel_type": "invalid"}) from e
                ch_stmt = ch_stmt.where(Channel.type == ctype)

        channel = db.execute(ch_stmt).scalar_one_or_none()
        if channel is None:
            raise HTTPException(status_code=404, detail="channel_not_found")

    stmt = select(Post, Channel).join(Channel, Channel.id == Post.channel_id)
    if channel is not None:
        stmt = stmt.where(Post.channel_id == channel.id)
    if dt_from is not None:
        stmt = stmt.where(Post.published_at >= dt_from)
    if dt_to is not None:
        stmt = stmt.where(Post.published_at <= dt_to)

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(stmt.order_by(Post.published_at.desc()).limit(limit).offset(offset)).all()

    items: list[PostOut] = []
    for p, ch in rows:
        ch_out = ChannelOut(
            id=ch.id,
            type=ch.type.value,
            identifier=ch.identifier,
            title=ch.title,
            is_active=ch.is_active,
            access_status=ch.access_status.value,
            backfill_days=ch.backfill_days,
            peer_id=ch.peer_id,
            last_checked_at=ch.last_checked_at,
            last_error=ch.last_error,
        )
        items.append(
            PostOut(
                id=p.id,
                channel=ch_out,
                original_url=p.original_url,
                published_at=p.published_at,
                text=p.text,
            )
        )

    return {"total": int(total), "limit": limit, "offset": offset, "items": items}
