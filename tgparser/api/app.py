from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from tgparser.models import Channel, ChannelType, Post

from .deps import get_db, require_token
from .schemas import ChannelOut, ChannelUpsertIn, ChannelsListResponse, PostOut, PostsListResponse

app = FastAPI(title="tgParser HTTP API", version="v1")


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


@app.get("/health")
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
    if channel_id is None and not channel_identifier:
        raise HTTPException(status_code=400, detail="channel_id_or_channel_identifier_required")

    dt_from = _parse_dt(date_from, field="date_from")
    dt_to = _parse_dt(date_to, field="date_to")

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

    stmt = select(Post).where(Post.channel_id == channel.id)
    if dt_from is not None:
        stmt = stmt.where(Post.published_at >= dt_from)
    if dt_to is not None:
        stmt = stmt.where(Post.published_at <= dt_to)

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    posts = db.execute(stmt.order_by(Post.published_at.desc()).limit(limit).offset(offset)).scalars().all()

    ch_out = ChannelOut(
        id=channel.id,
        type=channel.type.value,
        identifier=channel.identifier,
        title=channel.title,
        is_active=channel.is_active,
        access_status=channel.access_status.value,
        backfill_days=channel.backfill_days,
        peer_id=channel.peer_id,
        last_checked_at=channel.last_checked_at,
        last_error=channel.last_error,
    )

    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "items": [
            PostOut(
                id=p.id,
                channel=ch_out,
                original_url=p.original_url,
                published_at=p.published_at,
                text=p.text,
            )
            for p in posts
        ],
    }
