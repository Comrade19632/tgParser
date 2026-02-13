from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class AccountStatus(str, enum.Enum):
    active = "active"
    cooldown = "cooldown"
    banned = "banned"
    auth_required = "auth_required"
    error = "error"


class ChannelType(str, enum.Enum):
    public = "public"
    private = "private"


class ChannelAccessStatus(str, enum.Enum):
    active = "active"
    join_requested = "join_requested"
    pending_approval = "pending_approval"
    joined = "joined"
    forbidden = "forbidden"
    error = "error"


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Human-friendly label shown in bot lists. Default: phone number.
    label: Mapped[str] = mapped_column(String(128), default="")
    phone_number: Mapped[str] = mapped_column(String(32), default="")

    onboarding_method: Mapped[str] = mapped_column(String(32), default="")  # phone-code|tdata

    # Soft-disable flag for operator control.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    status: Mapped[AccountStatus] = mapped_column(Enum(AccountStatus), default=AccountStatus.active)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")

    # Telethon session: prefer StringSession stored in DB
    session_string: Mapped[str] = mapped_column(Text, default="")

    # Telethon API credentials (per account, like tgreact)
    api_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    type: Mapped[ChannelType] = mapped_column(Enum(ChannelType))
    identifier: Mapped[str] = mapped_column(String(255))  # username or invite hash
    title: Mapped[str] = mapped_column(String(255), default="")

    # Soft-disable flag for operator control.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    backfill_days: Mapped[int] = mapped_column(Integer, default=0)

    access_status: Mapped[ChannelAccessStatus] = mapped_column(
        Enum(ChannelAccessStatus), default=ChannelAccessStatus.active
    )

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cursor_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")

    posts: Mapped[list[Post]] = relationship(back_populates="channel")  # type: ignore[name-defined]

    __table_args__ = (
        UniqueConstraint("type", "identifier", name="uq_channel_type_identifier"),
    )


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)

    message_id: Mapped[int] = mapped_column(Integer)
    original_url: Mapped[str] = mapped_column(String(500), default="")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    text: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    channel: Mapped[Channel] = relationship(back_populates="posts")

    __table_args__ = (
        UniqueConstraint("channel_id", "message_id", name="uq_post_channel_message"),
        Index("ix_posts_original_url", "original_url"),
        Index("ix_posts_channel_published_at", "channel_id", "published_at"),
    )
