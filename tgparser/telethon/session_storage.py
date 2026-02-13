from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Account


class SessionStorageError(RuntimeError):
    pass


class SessionStorage:
    """Session storage abstraction.

    v1 decision: store Telethon StringSession in Postgres (Account.session_string).

    Keeping an explicit abstraction makes it easier to:
    - add encryption/rotation
    - add alternate storage backends
    - reuse in onboarding flows (phone-code/tdata)
    """

    def get_session_string(self, *, account_id: int) -> str:  # pragma: no cover (interface)
        raise NotImplementedError

    def set_session_string(self, *, account_id: int, session_string: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def clear_session_string(self, *, account_id: int) -> None:  # pragma: no cover
        raise NotImplementedError


@dataclass(frozen=True)
class DbSessionStorage(SessionStorage):
    """DB-backed storage using the accounts.session_string field."""

    def get_session_string(self, *, account_id: int) -> str:
        with SessionLocal() as db:
            acc = db.execute(select(Account).where(Account.id == account_id)).scalar_one_or_none()
            if not acc:
                raise SessionStorageError(f"Account not found: {account_id}")
            return (acc.session_string or "").strip()

    def set_session_string(self, *, account_id: int, session_string: str) -> None:
        session_string = (session_string or "").strip()
        with SessionLocal() as db:
            acc = db.execute(select(Account).where(Account.id == account_id)).scalar_one_or_none()
            if not acc:
                raise SessionStorageError(f"Account not found: {account_id}")
            acc.session_string = session_string
            db.commit()

    def clear_session_string(self, *, account_id: int) -> None:
        self.set_session_string(account_id=account_id, session_string="")
