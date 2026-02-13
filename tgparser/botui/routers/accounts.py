from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from ...db import SessionLocal
from ...models import Account, AccountStatus
from ...telethon.account_service import TelethonAccountService
from ...telethon.onboarding import (
    TelethonDeviceProfile,
    generate_device_profile,
    phone_code_finish,
    phone_code_start,
    tdata_to_session_string,
)
from ...telethon.session_storage import DbSessionStorage
from ...utils.tdata import TdataArchiveError, extract_tdata_from_archive
from .. import callbacks as cb

log = logging.getLogger(__name__)

router = Router()


def accounts_actions_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Add (phone-code)", callback_data=cb.ACC_ADD_PHONE)
    kb.button(text="Add (tdata)", callback_data=cb.ACC_ADD_TDATA)
    kb.button(text="List", callback_data=cb.ACC_LIST)
    kb.adjust(2, 1)
    kb.button(text="← Back", callback_data=cb.MAIN)
    kb.button(text="Refresh", callback_data=cb.ACCOUNTS)
    kb.adjust(2, 2)
    return kb


def account_row_kb(*, account_id: int, is_active: bool) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    if is_active:
        kb.button(text="Disable", callback_data=f"{cb.ACC_DISABLE}:{account_id}")
    kb.button(text="Remove", callback_data=f"{cb.ACC_REMOVE}:{account_id}")
    kb.adjust(2)
    return kb


class PhoneCodeFlow(StatesGroup):
    proxy = State()
    phone = State()
    code = State()
    two_fa = State()


class TdataFlow(StatesGroup):
    proxy = State()
    two_fa = State()
    tdata = State()


@dataclass(frozen=True)
class FlowProfile:
    profile: TelethonDeviceProfile
    proxy_url: str | None


async def _render_accounts_menu(q: CallbackQuery) -> None:
    if q.message:
        await q.message.edit_text(
            "Accounts\n\nChoose an action:",
            reply_markup=accounts_actions_kb().as_markup(),
        )


@router.callback_query(lambda q: q.data == cb.ACCOUNTS)
async def accounts_menu(q: CallbackQuery) -> None:
    await q.answer()
    await _render_accounts_menu(q)


@router.callback_query(lambda q: q.data == cb.ACC_ADD_PHONE)
async def acc_add_phone_start(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    await state.clear()

    profile = generate_device_profile(app_version="5.8.3 x64")
    await state.update_data(profile=profile.__dict__, proxy_url=None)

    if q.message:
        await q.message.answer(
            "Phone-code onboarding.\n\nSend proxy as http://user:pass@ip:port or /skip to continue without proxy.\n/cancel to abort."
        )

    await state.set_state(PhoneCodeFlow.proxy)


@router.message(PhoneCodeFlow.proxy, Command("skip"))
async def acc_add_phone_proxy_skip(m: Message, state: FSMContext) -> None:
    await state.update_data(proxy_url=None)
    await m.answer("Send phone number (international format, e.g. +79991234567). /cancel to abort")
    await state.set_state(PhoneCodeFlow.phone)


@router.message(PhoneCodeFlow.proxy, F.text)
async def acc_add_phone_proxy_set(m: Message, state: FSMContext) -> None:
    proxy_url = (m.text or "").strip()
    # Basic validation: store as-is, real parsing happens in telethon onboarding.
    await state.update_data(proxy_url=proxy_url)
    await m.answer("Send phone number (international format, e.g. +79991234567). /cancel to abort")
    await state.set_state(PhoneCodeFlow.phone)


@router.message(PhoneCodeFlow.phone, F.text)
async def acc_add_phone_phone(m: Message, state: FSMContext) -> None:
    phone = (m.text or "").strip().replace(" ", "")

    data = await state.get_data()
    profile = TelethonDeviceProfile(**data["profile"])
    proxy_url = data.get("proxy_url")

    # Start login
    try:
        start_info = await phone_code_start(phone_number=phone, profile=profile, proxy_url=proxy_url)
    except Exception as e:
        await m.answer(f"Failed to send code: {type(e).__name__}: {e}")
        await state.clear()
        return

    await state.update_data(
        phone_number=phone,
        session_string=start_info["session_string"],
        phone_code_hash=start_info["phone_code_hash"],
    )

    await m.answer("Code sent. Reply with the login code. /cancel to abort")
    await state.set_state(PhoneCodeFlow.code)


@router.message(PhoneCodeFlow.code, F.text)
async def acc_add_phone_code(m: Message, state: FSMContext) -> None:
    code = (m.text or "").strip()
    data = await state.get_data()

    profile = TelethonDeviceProfile(**data["profile"])
    proxy_url = data.get("proxy_url")

    try:
        session_string = await phone_code_finish(
            phone_number=data["phone_number"],
            profile=profile,
            proxy_url=proxy_url,
            session_string=data["session_string"],
            phone_code_hash=data["phone_code_hash"],
            code=code,
            two_fa=None,
        )
    except Exception as e:
        # 2FA required is a common case
        from telethon.errors import SessionPasswordNeededError

        if isinstance(e, SessionPasswordNeededError):
            await state.update_data(code=code)
            await m.answer("2FA password required. Send it now. /cancel to abort")
            await state.set_state(PhoneCodeFlow.two_fa)
            return

        await m.answer(f"Login failed: {type(e).__name__}: {e}")
        await state.clear()
        return

    await _create_account(
        m=m,
        onboarding_method="phone-code",
        phone_number=data["phone_number"],
        api_id=profile.api_id,
        api_hash=profile.api_hash,
        session_string=session_string,
    )

    await state.clear()


@router.message(PhoneCodeFlow.two_fa, F.text)
async def acc_add_phone_two_fa(m: Message, state: FSMContext) -> None:
    two_fa = (m.text or "").strip()
    data = await state.get_data()

    profile = TelethonDeviceProfile(**data["profile"])
    proxy_url = data.get("proxy_url")

    try:
        session_string = await phone_code_finish(
            phone_number=data["phone_number"],
            profile=profile,
            proxy_url=proxy_url,
            session_string=data["session_string"],
            phone_code_hash=data["phone_code_hash"],
            code=data.get("code", ""),
            two_fa=two_fa,
        )
    except Exception as e:
        await m.answer(f"Login failed: {type(e).__name__}: {e}")
        await state.clear()
        return

    await _create_account(
        m=m,
        onboarding_method="phone-code",
        phone_number=data["phone_number"],
        api_id=profile.api_id,
        api_hash=profile.api_hash,
        session_string=session_string,
    )

    await state.clear()


@router.callback_query(lambda q: q.data == cb.ACC_ADD_TDATA)
async def acc_add_tdata_start(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    await state.clear()

    profile = generate_device_profile(app_version="5.8.3 x64")
    await state.update_data(profile=profile.__dict__, proxy_url=None, two_fa=None)

    if q.message:
        await q.message.answer(
            "tdata onboarding.\n\nSend proxy as http://user:pass@ip:port or /skip to continue without proxy.\n/cancel to abort."
        )

    await state.set_state(TdataFlow.proxy)


@router.message(TdataFlow.proxy, Command("skip"))
async def acc_add_tdata_proxy_skip(m: Message, state: FSMContext) -> None:
    await state.update_data(proxy_url=None)
    await m.answer("If account has 2FA, send password now; or /skip to continue without 2FA")
    await state.set_state(TdataFlow.two_fa)


@router.message(TdataFlow.proxy, F.text)
async def acc_add_tdata_proxy_set(m: Message, state: FSMContext) -> None:
    await state.update_data(proxy_url=(m.text or "").strip())
    await m.answer("If account has 2FA, send password now; or /skip to continue without 2FA")
    await state.set_state(TdataFlow.two_fa)


@router.message(TdataFlow.two_fa, Command("skip"))
async def acc_add_tdata_two_fa_skip(m: Message, state: FSMContext) -> None:
    await state.update_data(two_fa=None)
    await m.answer("Send tdata archive as a .zip file (document).")
    await state.set_state(TdataFlow.tdata)


@router.message(TdataFlow.two_fa, F.text)
async def acc_add_tdata_two_fa_set(m: Message, state: FSMContext) -> None:
    await state.update_data(two_fa=(m.text or "").strip())
    await m.answer("Send tdata archive as a .zip file (document).")
    await state.set_state(TdataFlow.tdata)


@router.message(TdataFlow.tdata, F.document)
async def acc_add_tdata_file(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    profile = TelethonDeviceProfile(**data["profile"])

    doc = m.document
    if not doc:
        await m.answer("No document found")
        return

    # Save upload to a temp file
    tmp_dir = tempfile.mkdtemp(prefix="tgparser_upload_")
    archive_path = os.path.join(tmp_dir, doc.file_name or "tdata.zip")

    try:
        f = await m.bot.get_file(doc.file_id)
        await m.bot.download_file(f.file_path, destination=archive_path)

        tdata_folder = extract_tdata_from_archive(archive_path=archive_path)

        res = await tdata_to_session_string(
            tdata_folder=tdata_folder,
            profile=profile,
            proxy_url=data.get("proxy_url"),
            two_fa=data.get("two_fa"),
        )

        phone_number = (res.get("phone_number") or "").strip()
        session_string = res["session_string"]

        await _create_account(
            m=m,
            onboarding_method="tdata",
            phone_number=phone_number,
            api_id=profile.api_id,
            api_hash=profile.api_hash,
            session_string=session_string,
        )
    except (TdataArchiveError, FileNotFoundError) as e:
        await m.answer(f"tdata archive error: {e}")
    except Exception as e:
        await m.answer(f"tdata onboarding failed: {type(e).__name__}: {e}")
    finally:
        await state.clear()


@router.message(Command("cancel"))
async def flow_cancel(m: Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer("Cancelled.")


async def _create_account(
    *,
    m: Message,
    onboarding_method: str,
    phone_number: str,
    api_id: int,
    api_hash: str,
    session_string: str,
) -> None:
    phone_number = (phone_number or "").strip()
    label = phone_number or f"acc-{onboarding_method}"

    with SessionLocal() as db:
        # Avoid duplicates by phone if we have it.
        if phone_number:
            existing = db.execute(select(Account).where(Account.phone_number == phone_number)).scalar_one_or_none()
            if existing and existing.is_active:
                await m.answer("Account already exists and is active")
                return

        acc = Account(
            label=label,
            phone_number=phone_number,
            onboarding_method=onboarding_method,
            is_active=True,
            status=AccountStatus.active,
            session_string=session_string,
            api_id=api_id,
            api_hash=api_hash,
        )
        db.add(acc)
        db.commit()

    await m.answer(f"Account added: {label}")


@router.callback_query(lambda q: q.data == cb.ACC_LIST)
async def acc_list(q: CallbackQuery) -> None:
    await q.answer()

    # Optionally refresh statuses (best-effort)
    service = TelethonAccountService(session_storage=DbSessionStorage())

    with SessionLocal() as db:
        accounts = list(db.execute(select(Account).order_by(Account.id.asc())).scalars())

        if not accounts:
            if q.message:
                await q.message.answer("No accounts yet.")
            return

        lines: list[str] = []
        for acc in accounts:
            status = acc.status
            last_error = (acc.last_error or "").strip()

            if acc.is_active:
                try:
                    health = await service.check(account_id=acc.id)
                    acc.status = health.status
                    acc.last_error = health.last_error
                    acc.cooldown_until = health.cooldown_until
                    status = health.status
                    last_error = (health.last_error or "").strip()
                except Exception as e:
                    acc.status = AccountStatus.error
                    acc.last_error = f"{type(e).__name__}: {e}"
                    status = acc.status
                    last_error = acc.last_error

            active_flag = "active" if acc.is_active else "disabled"
            label = acc.label or acc.phone_number or f"acc#{acc.id}"
            tail = f" — {last_error}" if last_error else ""
            lines.append(f"#{acc.id} [{active_flag}] {label} — {status}{tail}")

        db.commit()

    if q.message:
        await q.message.answer("Accounts:\n" + "\n".join(lines))


@router.callback_query(F.data.startswith(f"{cb.ACC_DISABLE}:"))
async def acc_disable(q: CallbackQuery) -> None:
    await q.answer()
    try:
        account_id = int((q.data or "").split(":", 2)[2])
    except Exception:
        return

    with SessionLocal() as db:
        acc = db.get(Account, account_id)
        if not acc:
            if q.message:
                await q.message.answer("Account not found")
            return
        acc.is_active = False
        db.commit()

    if q.message:
        await q.message.answer(f"Account #{account_id} disabled")


@router.callback_query(F.data.startswith(f"{cb.ACC_REMOVE}:"))
async def acc_remove(q: CallbackQuery) -> None:
    await q.answer()
    try:
        account_id = int((q.data or "").split(":", 2)[2])
    except Exception:
        return

    with SessionLocal() as db:
        acc = db.get(Account, account_id)
        if not acc:
            if q.message:
                await q.message.answer("Account not found")
            return
        db.delete(acc)
        db.commit()

    if q.message:
        await q.message.answer(f"Account #{account_id} removed")
