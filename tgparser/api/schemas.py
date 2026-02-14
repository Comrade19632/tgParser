from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


ChannelType = Literal["public", "private"]


class ChannelOut(BaseModel):
    id: int
    type: ChannelType
    identifier: str
    title: str
    is_active: bool
    access_status: str
    backfill_days: int
    peer_id: int | None = None
    last_checked_at: datetime | None = None
    last_error: str


class ChannelUpsertIn(BaseModel):
    type: ChannelType
    identifier: str = Field(min_length=1, max_length=255)
    backfill_days: int = Field(default=0, ge=0, le=3650)
    is_active: bool = True


class PostOut(BaseModel):
    id: int
    channel: ChannelOut
    original_url: str
    published_at: datetime
    text: str


class ListResponse(BaseModel):
    total: int
    limit: int
    offset: int


class ChannelsListResponse(ListResponse):
    items: list[ChannelOut]


class PostsListResponse(ListResponse):
    items: list[PostOut]
