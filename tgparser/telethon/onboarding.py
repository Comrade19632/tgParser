from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Any

import socks
from opentele.api import API, CreateNewSession
from opentele.td import TDesktop
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon import errors

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelethonDeviceProfile:
    api_id: int
    api_hash: str
    device_model: str
    system_version: str
    app_version: str
    lang_code: str
    system_lang_code: str


def generate_device_profile(*, app_version: str) -> TelethonDeviceProfile:
    api = API.TelegramDesktop.Generate()
    return TelethonDeviceProfile(
        api_id=int(api.api_id),
        api_hash=str(api.api_hash),
        device_model=str(api.device_model),
        system_version=str(api.system_version),
        app_version=app_version,
        lang_code=str(api.lang_code),
        system_lang_code=str(api.system_lang_code),
    )


def parse_proxy_url(proxy_url: str) -> tuple[Any, ...]:
    """Parse proxy url like: http://user:pass@ip:port

    Returns Telethon-compatible proxy tuple.
    """

    from urllib.parse import urlparse

    p = urlparse((proxy_url or "").strip())
    if p.scheme not in {"http"}:
        raise ValueError("Proxy scheme must be http://")
    if not p.hostname or not p.port:
        raise ValueError("Proxy must include host:port")

    username = p.username or ""
    password = p.password or ""

    return (
        socks.HTTP,
        p.hostname,
        int(p.port),
        True,
        username,
        password,
    )


async def phone_code_start(
    *,
    phone_number: str,
    profile: TelethonDeviceProfile,
    proxy_url: str | None,
) -> dict[str, str]:
    """Start phone-code login.

    Returns dict with:
    - session_string: current session state to continue
    - phone_code_hash: for sign_in
    """

    proxy = parse_proxy_url(proxy_url) if proxy_url else None

    client = TelegramClient(
        StringSession(),
        api_id=profile.api_id,
        api_hash=profile.api_hash,
        device_model=profile.device_model,
        system_version=profile.system_version,
        app_version=profile.app_version,
        lang_code=profile.lang_code,
        system_lang_code=profile.system_lang_code,
        proxy=proxy,
        connection_retries=1,
        retry_delay=1,
    )

    try:
        await client.connect()
        sent = await client.send_code_request(phone=phone_number)
        sess = client.session.save()
        return {"session_string": sess, "phone_code_hash": sent.phone_code_hash}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log.exception("telethon: disconnect failed (phone_code_start)")


async def phone_code_finish(
    *,
    phone_number: str,
    profile: TelethonDeviceProfile,
    proxy_url: str | None,
    session_string: str,
    phone_code_hash: str,
    code: str,
    two_fa: str | None,
) -> str:
    proxy = parse_proxy_url(proxy_url) if proxy_url else None

    client = TelegramClient(
        StringSession(session_string),
        api_id=profile.api_id,
        api_hash=profile.api_hash,
        device_model=profile.device_model,
        system_version=profile.system_version,
        app_version=profile.app_version,
        lang_code=profile.lang_code,
        system_lang_code=profile.system_lang_code,
        proxy=proxy,
        connection_retries=1,
        retry_delay=1,
    )

    try:
        await client.connect()

        try:
            await client.sign_in(
                phone=phone_number,
                code=code,
                phone_code_hash=phone_code_hash,
            )
        except errors.SessionPasswordNeededError:
            if not two_fa:
                raise
            await client.sign_in(password=two_fa)

        return client.session.save()
    finally:
        try:
            await client.disconnect()
        except Exception:
            log.exception("telethon: disconnect failed (phone_code_finish)")


async def tdata_to_session_string(
    *,
    tdata_folder: str,
    profile: TelethonDeviceProfile,
    proxy_url: str | None,
    two_fa: str | None,
    timeout_seconds: int = 30,
) -> dict[str, str]:
    """Convert Telegram Desktop tdata folder to Telethon StringSession.

    Uses opentele (same approach as tgreact).
    """

    abs_tdata = os.path.abspath(tdata_folder)
    if not os.path.exists(abs_tdata):
        raise FileNotFoundError(f"tdata folder not found: {abs_tdata}")

    api = API.TelegramDesktop.Generate()
    api.api_id = profile.api_id
    api.api_hash = profile.api_hash
    api.device_model = profile.device_model
    api.system_version = profile.system_version
    api.app_version = profile.app_version
    api.lang_code = profile.lang_code
    api.system_lang_code = profile.system_lang_code

    proxy = parse_proxy_url(proxy_url) if proxy_url else None

    async def _run() -> dict[str, str]:
        tdesk = TDesktop(abs_tdata)

        kwargs: dict[str, Any] = {}
        if proxy is not None:
            # opentele expects same tuple as Telethon
            kwargs["proxy"] = proxy
        if two_fa:
            kwargs["password"] = two_fa

        client = await tdesk.ToTelethon(StringSession(), CreateNewSession, api, **kwargs)
        try:
            await client.connect()
            await client.sign_in()
            me = await client.get_me()
            phone = getattr(me, "phone", None) or ""
            return {"session_string": client.session.save(), "phone_number": phone}
        finally:
            try:
                await client.disconnect()
            except Exception:
                log.exception("telethon: disconnect failed (tdata_to_session_string)")

    return await asyncio.wait_for(_run(), timeout=timeout_seconds)


def prepare_tdata_upload_dir() -> str:
    base = os.path.join(tempfile.gettempdir(), "tgparser_tdata")
    os.makedirs(base, exist_ok=True)
    return base
