from __future__ import annotations

import argparse
from datetime import datetime, timezone

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Channel, ChannelAccessStatus, ChannelType
from ..botui.routers.channels import normalize_invite, normalize_public


def upsert_channel(*, ch_type: ChannelType, identifier: str, backfill_days: int) -> Channel:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        existing = db.execute(
            select(Channel).where(Channel.type == ch_type, Channel.identifier == identifier)
        ).scalar_one_or_none()

        if existing:
            existing.is_active = True
            existing.backfill_days = backfill_days
            if not existing.added_at:
                existing.added_at = now
            db.commit()
            db.refresh(existing)
            return existing

        ch = Channel(
            type=ch_type,
            identifier=identifier,
            title=identifier,  # will be replaced on first successful fetch
            added_at=now,
            backfill_days=backfill_days,
            access_status=ChannelAccessStatus.active,
            last_error="",
            is_active=True,
        )
        db.add(ch)
        db.commit()
        db.refresh(ch)
        return ch


def main() -> None:
    p = argparse.ArgumentParser(description="Seed channels into TG Parser DB (admin/ops tool).")
    p.add_argument("--public", action="append", default=[], help="Public channel: @username or https://t.me/username")
    p.add_argument(
        "--private",
        action="append",
        default=[],
        help="Private invite: https://t.me/+HASH or raw HASH",
    )
    p.add_argument(
        "--demo",
        action="store_true",
        help=(
            "Seed a curated set of known public channels for pagination/UI testing. "
            "(Adds to any explicitly provided --public/--private.)"
        ),
    )
    p.add_argument("--backfill-days", type=int, default=0, help="Backfill days for all added channels")
    args = p.parse_args()

    demo_public = [
        "@durov",
        "@telegram",
        "@telegramtips",
        "@telegramnews",
        "@tgbeta",
        "@botnews",
        "@toncoin",
        "@tonblockchain",
        "@techcrunch",
        "@verge",
        "@nytimes",
        "@bbcnews",
        "@reuters",
        "@bloomberg",
    ]

    public_inputs = list(args.public)
    private_inputs = list(args.private)
    if args.demo:
        public_inputs.extend(demo_public)

    added: list[Channel] = []

    for raw in public_inputs:
        ident = normalize_public(raw)
        if not ident:
            raise SystemExit(f"Invalid public identifier: {raw}")
        added.append(upsert_channel(ch_type=ChannelType.public, identifier=ident, backfill_days=args.backfill_days))

    for raw in private_inputs:
        ident = normalize_invite(raw)
        if not ident:
            raise SystemExit(f"Invalid private invite/hash: {raw}")
        added.append(upsert_channel(ch_type=ChannelType.private, identifier=ident, backfill_days=args.backfill_days))

    print(f"ok: seeded {len(added)} channels")
    for ch in added:
        print(f"- id={ch.id} type={ch.type.value} identifier={ch.identifier} backfill_days={ch.backfill_days} active={ch.is_active}")


if __name__ == "__main__":
    main()
