from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from tgparser.db import SessionLocal
from tgparser.settings import settings

_security = HTTPBearer(auto_error=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_token(creds: HTTPAuthorizationCredentials | None = Depends(_security)) -> None:
    # Fail-closed if token isn't configured.
    expected = (getattr(settings, "service_api_token", "") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="SERVICE_API_TOKEN_not_configured")

    token = (creds.credentials if creds else "").strip()
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")
