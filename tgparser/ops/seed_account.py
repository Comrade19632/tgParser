from __future__ import annotations

import argparse
from datetime import datetime, timezone

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Account, AccountStatus


def upsert_account(*, phone_number: str, label: str, api_id: int, api_hash: str, session_string: str) -> Account:
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        existing = db.execute(select(Account).where(Account.phone_number == phone_number)).scalar_one_or_none()

        if existing:
            existing.label = label or existing.label
            existing.api_id = api_id
            existing.api_hash = api_hash
            existing.session_string = session_string
            existing.is_active = True
            existing.status = AccountStatus.active
            existing.last_error = ""
            existing.updated_at = now
            db.commit()
            db.refresh(existing)
            return existing

        acc = Account(
            label=label or phone_number,
            phone_number=phone_number,
            onboarding_method="ops-seed",
            status=AccountStatus.active,
            cooldown_until=None,
            last_error="",
            session_string=session_string,
            api_id=api_id,
            api_hash=api_hash,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(acc)
        db.commit()
        db.refresh(acc)
        return acc


def main() -> None:
    p = argparse.ArgumentParser(description="Seed one Telethon account (StringSession) into TG Parser DB.")
    p.add_argument("--phone-number", required=True)
    p.add_argument("--label", default="")
    p.add_argument("--api-id", type=int, required=True)
    p.add_argument("--api-hash", required=True)
    p.add_argument("--session-string", required=True, help="Telethon StringSession")
    args = p.parse_args()

    acc = upsert_account(
        phone_number=args.phone_number,
        label=args.label,
        api_id=args.api_id,
        api_hash=args.api_hash,
        session_string=args.session_string,
    )
    print(f"ok: seeded account id={acc.id} phone={acc.phone_number} active={acc.is_active} status={acc.status.value}")


if __name__ == "__main__":
    main()
