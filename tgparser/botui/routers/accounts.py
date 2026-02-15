from __future__ import annotations

import logging
import os
import shutil
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
from ...models import Account, AccountChannelMembership, AccountChannelStatus, AccountStatus
from ...telethon.account_service import TelethonAccountService
from ...telethon.onboarding import (
    TelethonDeviceProfile,
    generate_device_profile,
    phone_code_finish,
    phone_code_start,
    tdata_to_session_string,
)
from ...telethon.session_storage import DbSessionStorage
from ...user_tracking import track_user
from ...utils.tdata import TdataArchiveError, extract_tdata_from_archive
from .. import callbacks as cb

log = logging.getLogger(__name__)

router = Router()


def accounts_actions_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Добавить (код)", callback_data=cb.ACC_ADD_PHONE)
    kb.button(text="Добавить (tdata)", callback_data=cb.ACC_ADD_TDATA)
    kb.button(text="Список", callback_data=f"{cb.ACC_LIST}:0")
    kb.adjust(2, 1)
    kb.button(text="← Назад", callback_data=cb.MAIN)
    kb.button(text="Обновить", callback_data=cb.ACCOUNTS)
    kb.adjust(2, 2)
    return kb


def account_row_kb(*, account_id: int, is_active: bool, page: int = 0) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    action = "Выключить" if is_active else "Включить"
    kb.button(text=action, callback_data=f"{cb.ACC_TOGGLE}:{account_id}:{page}")
    kb.button(text="Удалить", callback_data=f"{cb.ACC_REMOVE}:{account_id}:{page}")
    kb.adjust(2)
    return kb


class PhoneCodeFlow(StatesGroup):
    app = State()
    proxy = State()
    phone = State()
    code = State()
    two_fa = State()


class TdataFlow(StatesGroup):
    app = State()
    proxy = State()
    two_fa = State()
    tdata = State()


@dataclass(frozen=True)
class FlowProfile:
    profile: TelethonDeviceProfile
    proxy_url: str | None


async def _render_accounts_menu(q: CallbackQuery) -> None:
    prefix = ""
    with SessionLocal() as db:
        prefix = _counts_prefix_accounts(db=db)

    if q.message:
        await q.message.edit_text(
            "Аккаунты\n\n" + prefix + "Выберите действие:",
            reply_markup=accounts_actions_kb().as_markup(),
        )


@router.callback_query(lambda q: q.data == cb.ACCOUNTS)
async def accounts_menu(q: CallbackQuery) -> None:
    await q.answer()
    if q.from_user:
        track_user(q.from_user.id)
    await _render_accounts_menu(q)


@router.callback_query(lambda q: q.data == cb.ACC_ADD_PHONE)
async def acc_add_phone_start(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    await state.clear()

    profile = generate_device_profile(app_version="5.8.3 x64")
    await state.update_data(profile=profile.__dict__, proxy_url=None, reauth_account_id=None)

    if q.message:
        await q.message.answer(
            "Онбординг по коду.\n\n"
            "Если нужно — пришлите кастомные API креды приложения:\n"
            "APP <api_id> <api_hash>\n"
            "или /skip чтобы использовать дефолтные.\n/cancel — отмена."
        )

    await state.set_state(PhoneCodeFlow.app)


@router.callback_query(F.data.startswith(f"{cb.ACC_REAUTH_PHONE}:"))
async def acc_reauth_phone_start(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    await state.clear()

    data = (q.data or "")
    try:
        # callback format: "accounts:reauth:phone:<account_id>:<page>" (prefix contains ":")
        parts = data.split(":")
        account_id = int(parts[-2])
        _page = int(parts[-1])
    except Exception:
        await q.answer("Bad callback", show_alert=False)
        return

    profile = generate_device_profile(app_version="5.8.3 x64")

    # Try to reuse api creds / phone / proxy from previous authorization.
    with SessionLocal() as db:
        acc = db.get(Account, account_id)
        phone_number = (acc.phone_number or "").strip() if acc else ""
        proxy_url = (acc.proxy_url or "").strip() if acc else ""
        # Prefer stored api creds if present
        if acc and acc.api_id and acc.api_hash:
            p = profile.__dict__.copy()
            p["api_id"] = int(acc.api_id)
            p["api_hash"] = str(acc.api_hash)
            profile_dict = p
        else:
            profile_dict = profile.__dict__

    await state.update_data(profile=profile_dict, proxy_url=(proxy_url or None), reauth_account_id=account_id)

    if q.message:
        await q.message.answer(
            f"Переавторизация аккаунта #{account_id} (по коду).\n\n"
            "Если нужно — пришлите кастомные API креды приложения:\n"
            "APP <api_id> <api_hash>\n"
            "или /skip чтобы использовать сохранённые/дефолтные.\n/cancel — отмена."
        )

    await state.set_state(PhoneCodeFlow.app)


@router.message(PhoneCodeFlow.app, Command("skip"))
async def acc_add_phone_app_skip(m: Message, state: FSMContext) -> None:
    # Keep generated / stored profile as-is
    await m.answer(
        "Пришлите proxy (http://user:pass@ip:port) или /skip чтобы продолжить без proxy.\n/cancel — отмена."
    )
    await state.set_state(PhoneCodeFlow.proxy)


@router.message(PhoneCodeFlow.app, F.text)
async def acc_add_phone_app_set(m: Message, state: FSMContext) -> None:
    text = (m.text or "").strip()
    parts = text.split()
    if len(parts) != 3 or parts[0].upper() != "APP":
        await m.answer("Формат: APP <api_id> <api_hash> (или /skip)")
        return

    try:
        api_id = int(parts[1])
        api_hash = parts[2].strip()
        if not api_hash:
            raise ValueError("empty api_hash")
    except Exception:
        await m.answer("Не удалось распарсить. Формат: APP <api_id> <api_hash>")
        return

    data = await state.get_data()
    prof = dict(data.get("profile") or {})
    prof["api_id"] = api_id
    prof["api_hash"] = api_hash
    await state.update_data(profile=prof)

    await m.answer(
        "Ок. Теперь пришлите proxy (http://user:pass@ip:port) или /skip чтобы продолжить без proxy.\n/cancel — отмена."
    )
    await state.set_state(PhoneCodeFlow.proxy)


@router.message(PhoneCodeFlow.proxy, Command("skip"))
async def acc_add_phone_proxy_skip(m: Message, state: FSMContext) -> None:
    await state.update_data(proxy_url=None)
    await m.answer("Пришлите номер телефона в международном формате (например: +79991234567).\n/cancel — отмена.")
    await state.set_state(PhoneCodeFlow.phone)


@router.message(PhoneCodeFlow.proxy, F.text)
async def acc_add_phone_proxy_set(m: Message, state: FSMContext) -> None:
    proxy_url = (m.text or "").strip()
    # Basic validation: store as-is, real parsing happens in telethon onboarding.
    await state.update_data(proxy_url=proxy_url)
    await m.answer("Пришлите номер телефона в международном формате (например: +79991234567).\n/cancel — отмена.")
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
        await m.answer(f"Не удалось отправить код: {type(e).__name__}: {e}")
        await state.clear()
        return

    await state.update_data(
        phone_number=phone,
        session_string=start_info["session_string"],
        phone_code_hash=start_info["phone_code_hash"],
    )

    await m.answer("Код отправлен. Пришлите код входа.\n/cancel — отмена.")
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
            await m.answer("Нужен пароль 2FA. Пришлите его.\n/cancel — отмена.")
            await state.set_state(PhoneCodeFlow.two_fa)
            return

        await m.answer(f"Не удалось войти: {type(e).__name__}: {e}")
        await state.clear()
        return

    await _create_or_update_account(
        m=m,
        onboarding_method="phone-code",
        phone_number=data["phone_number"],
        api_id=profile.api_id,
        api_hash=profile.api_hash,
        session_string=session_string,
        proxy_url=data.get("proxy_url"),
        reauth_account_id=data.get("reauth_account_id"),
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
        await m.answer(f"Не удалось войти: {type(e).__name__}: {e}")
        await state.clear()
        return

    await _create_or_update_account(
        m=m,
        onboarding_method="phone-code",
        phone_number=data["phone_number"],
        api_id=profile.api_id,
        api_hash=profile.api_hash,
        session_string=session_string,
        proxy_url=data.get("proxy_url"),
        reauth_account_id=data.get("reauth_account_id"),
    )

    await state.clear()


@router.callback_query(lambda q: q.data == cb.ACC_ADD_TDATA)
async def acc_add_tdata_start(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    await state.clear()

    profile = generate_device_profile(app_version="5.8.3 x64")
    await state.update_data(profile=profile.__dict__, proxy_url=None, two_fa=None, reauth_account_id=None)

    if q.message:
        await q.message.answer(
            "Онбординг через tdata.\n\n"
            "Если нужно — пришлите кастомные API креды приложения:\n"
            "APP <api_id> <api_hash>\n"
            "или /skip чтобы использовать дефолтные.\n/cancel — отмена."
        )

    await state.set_state(TdataFlow.app)


@router.callback_query(F.data.startswith(f"{cb.ACC_REAUTH_TDATA}:"))
async def acc_reauth_tdata_start(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    await state.clear()

    data = (q.data or "")
    try:
        # callback format: "accounts:reauth:tdata:<account_id>:<page>" (prefix contains ":")
        parts = data.split(":")
        account_id = int(parts[-2])
        _page = int(parts[-1])
    except Exception:
        await q.answer("Bad callback", show_alert=False)
        return

    profile = generate_device_profile(app_version="5.8.3 x64")

    with SessionLocal() as db:
        acc = db.get(Account, account_id)
        proxy_url = (acc.proxy_url or "").strip() if acc else ""
        if acc and acc.api_id and acc.api_hash:
            p = profile.__dict__.copy()
            p["api_id"] = int(acc.api_id)
            p["api_hash"] = str(acc.api_hash)
            profile_dict = p
        else:
            profile_dict = profile.__dict__

    await state.update_data(profile=profile_dict, proxy_url=(proxy_url or None), two_fa=None, reauth_account_id=account_id)

    if q.message:
        await q.message.answer(
            f"Переавторизация аккаунта #{account_id} (через tdata).\n\n"
            "Если нужно — пришлите кастомные API креды приложения:\n"
            "APP <api_id> <api_hash>\n"
            "или /skip чтобы использовать сохранённые/дефолтные.\n/cancel — отмена."
        )

    await state.set_state(TdataFlow.app)


@router.message(TdataFlow.app, Command("skip"))
async def acc_add_tdata_app_skip(m: Message, state: FSMContext) -> None:
    await m.answer(
        "Пришлите proxy (http://user:pass@ip:port) или /skip чтобы продолжить без proxy.\n/cancel — отмена."
    )
    await state.set_state(TdataFlow.proxy)


@router.message(TdataFlow.app, F.text)
async def acc_add_tdata_app_set(m: Message, state: FSMContext) -> None:
    text = (m.text or "").strip()
    parts = text.split()
    if len(parts) != 3 or parts[0].upper() != "APP":
        await m.answer("Формат: APP <api_id> <api_hash> (или /skip)")
        return

    try:
        api_id = int(parts[1])
        api_hash = parts[2].strip()
        if not api_hash:
            raise ValueError("empty api_hash")
    except Exception:
        await m.answer("Не удалось распарсить. Формат: APP <api_id> <api_hash>")
        return

    data = await state.get_data()
    prof = dict(data.get("profile") or {})
    prof["api_id"] = api_id
    prof["api_hash"] = api_hash
    await state.update_data(profile=prof)

    await m.answer(
        "Ок. Теперь пришлите proxy (http://user:pass@ip:port) или /skip чтобы продолжить без proxy.\n/cancel — отмена."
    )
    await state.set_state(TdataFlow.proxy)


@router.message(TdataFlow.proxy, Command("skip"))
async def acc_add_tdata_proxy_skip(m: Message, state: FSMContext) -> None:
    await state.update_data(proxy_url=None)
    await m.answer("Если у аккаунта есть 2FA — пришлите пароль сейчас, или /skip чтобы продолжить без 2FA")
    await state.set_state(TdataFlow.two_fa)


@router.message(TdataFlow.proxy, F.text)
async def acc_add_tdata_proxy_set(m: Message, state: FSMContext) -> None:
    await state.update_data(proxy_url=(m.text or "").strip())
    await m.answer("Если у аккаунта есть 2FA — пришлите пароль сейчас, или /skip чтобы продолжить без 2FA")
    await state.set_state(TdataFlow.two_fa)


@router.message(TdataFlow.two_fa, Command("skip"))
async def acc_add_tdata_two_fa_skip(m: Message, state: FSMContext) -> None:
    await state.update_data(two_fa=None)
    await m.answer(
        "Send Telegram Desktop tdata as a .zip file (document).\n\n"
        "How to prepare:\n"
        "1) On a machine with Telegram Desktop logged in, locate the 'tdata' folder.\n"
        "2) Zip it so that archive contains a top-level 'tdata/' directory (not just the files).\n"
        "3) Send the .zip here as a document.\n\n"
        "Security note: tdata contains active session material. Treat it like a password.\n"
        "/cancel to abort."
    )
    await state.set_state(TdataFlow.tdata)


@router.message(TdataFlow.two_fa, F.text)
async def acc_add_tdata_two_fa_set(m: Message, state: FSMContext) -> None:
    await state.update_data(two_fa=(m.text or "").strip())
    await m.answer(
        "Send Telegram Desktop tdata as a .zip file (document).\n\n"
        "How to prepare:\n"
        "1) On a machine with Telegram Desktop logged in, locate the 'tdata' folder.\n"
        "2) Zip it so that archive contains a top-level 'tdata/' directory (not just the files).\n"
        "3) Send the .zip here as a document.\n\n"
        "Security note: tdata contains active session material. Treat it like a password.\n"
        "/cancel to abort."
    )
    await state.set_state(TdataFlow.tdata)


@router.message(TdataFlow.tdata, F.document)
async def acc_add_tdata_file(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    profile = TelethonDeviceProfile(**data["profile"])

    doc = m.document
    if not doc:
        await m.answer("No document found")
        return

    # Basic guardrail: keep uploads reasonably small (Telegram Desktop tdata is usually a few MB).
    if doc.file_size and doc.file_size > 25 * 1024 * 1024:
        await m.answer(
            "tdata.zip is too large for this flow (limit: 25MB). "
            "Please zip only the 'tdata' folder from Telegram Desktop and try again."
        )
        await state.clear()
        return

    # Save upload to a temp file
    tmp_dir = tempfile.mkdtemp(prefix="tgparser_upload_")
    archive_path = os.path.join(tmp_dir, doc.file_name or "tdata.zip")

    try:
        f = await m.bot.get_file(doc.file_id)
        await m.bot.download_file(f.file_path, destination=archive_path)

        extract_root = os.path.join(tmp_dir, "extracted")
        tdata_folder = extract_tdata_from_archive(archive_path=archive_path, extract_root=extract_root)

        res = await tdata_to_session_string(
            tdata_folder=tdata_folder,
            profile=profile,
            proxy_url=data.get("proxy_url"),
            two_fa=data.get("two_fa"),
        )

        phone_number = (res.get("phone_number") or "").strip()
        session_string = res["session_string"]

        await _create_or_update_account(
            m=m,
            onboarding_method="tdata",
            phone_number=phone_number,
            api_id=profile.api_id,
            api_hash=profile.api_hash,
            session_string=session_string,
            proxy_url=data.get("proxy_url"),
            reauth_account_id=data.get("reauth_account_id"),
        )
    except (TdataArchiveError, FileNotFoundError) as e:
        await m.answer(f"tdata archive error: {e}")
    except Exception as e:
        await m.answer(f"tdata onboarding failed: {type(e).__name__}: {e}")
    finally:
        # Best-effort cleanup of secrets (tdata archive + extracted folder)
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        await state.clear()


@router.message(TdataFlow.tdata)
async def acc_add_tdata_wrong_payload(m: Message) -> None:
    await m.answer(
        "Waiting for a .zip document with Telegram Desktop 'tdata'.\n"
        "Please send it as a document attachment (not as text / photo).\n"
        "/cancel to abort."
    )


@router.message(Command("cancel"))
async def flow_cancel(m: Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer("Cancelled.")


async def _create_or_update_account(
    *,
    m: Message,
    onboarding_method: str,
    phone_number: str,
    api_id: int,
    api_hash: str,
    session_string: str,
    proxy_url: str | None = None,
    reauth_account_id: int | None = None,
) -> None:
    phone_number = (phone_number or "").strip()
    label = phone_number or f"acc-{onboarding_method}"

    with SessionLocal() as db:
        # Re-auth path: overwrite session for the existing account.
        if reauth_account_id is not None:
            acc = db.get(Account, int(reauth_account_id))
            if not acc:
                await m.answer(f"Account not found: {reauth_account_id}")
                return

            acc.onboarding_method = onboarding_method
            if phone_number:
                acc.phone_number = phone_number
            if label:
                acc.label = label

            acc.is_active = True
            acc.status = AccountStatus.active
            acc.session_string = session_string
            acc.api_id = api_id
            acc.api_hash = api_hash
            if proxy_url is not None:
                acc.proxy_url = proxy_url
            acc.last_error = ""
            acc.cooldown_until = None
            db.commit()

            await m.answer(f"Account re-authorized: {acc.label or acc.phone_number or acc.id}")
            return

        # Create path
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
            proxy_url=proxy_url or "",
            status=AccountStatus.active,
            session_string=session_string,
            api_id=api_id,
            api_hash=api_hash,
        )
        db.add(acc)
        db.commit()

    await m.answer(f"Account added: {label}")


PAGE_SIZE = 6


def _counts_prefix_accounts(*, db) -> str:
    # Hide soft-removed accounts (Remove action): is_active=false + status=forbidden + cleared session.
    not_removed = ~(
        (Account.is_active.is_(False))
        & (Account.status == AccountStatus.forbidden)
        & (Account.session_string == "")
    )

    total = db.execute(select(Account.id).where(not_removed)).all()
    enabled = db.execute(select(Account.id).where(not_removed, Account.is_active.is_(True))).all()
    usable = db.execute(
        select(Account.id).where(not_removed, Account.is_active.is_(True), Account.status == AccountStatus.active)
    ).all()

    # Semantics:
    # - enabled: toggled ON by user
    # - usable: enabled AND Telethon session is authorized (status=active)
    return f"Usable/Enabled/Total: {len(usable)}/{len(enabled)}/{len(total)}\n\n"


def _accounts_list_kb(*, accounts: list[Account], page: int, total_pages: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()

    for a in accounts:
        icon = "✅" if a.is_active else "⛔"
        label = (a.label or a.phone_number or f"acc#{a.id}").strip()
        text = f"{icon} #{a.id} {label}"
        kb.button(text=text[:64], callback_data=f"{cb.ACC_VIEW}:{a.id}:{page}")

    kb.adjust(1)

    prev_page = max(0, page - 1)
    next_page = min(total_pages - 1, page + 1)

    kb.button(text="◀ Prev", callback_data=f"{cb.ACC_LIST}:{prev_page}")
    kb.button(text=f"{page + 1}/{total_pages}", callback_data="noop")
    kb.button(text="Next ▶", callback_data=f"{cb.ACC_LIST}:{next_page}")
    kb.adjust(3)

    kb.button(text="← Аккаунты", callback_data=cb.ACCOUNTS)
    kb.button(text="Обновить", callback_data=f"{cb.ACC_LIST}:{page}")
    kb.adjust(2)

    return kb


def _account_detail_kb(
    *, account_id: int, is_active: bool, status: AccountStatus | None, page: int
) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()

    # If session is not authorized, offer re-auth flows that UPDATE session_string for this account.
    if status == AccountStatus.auth_required:
        kb.button(text="🔐 Переавторизовать (код)", callback_data=f"{cb.ACC_REAUTH_PHONE}:{account_id}:{page}")
        kb.button(text="🔐 Переавторизовать (tdata)", callback_data=f"{cb.ACC_REAUTH_TDATA}:{account_id}:{page}")
        kb.adjust(1, 1)

    action = "Выключить" if is_active else "Включить"
    kb.button(text=action, callback_data=f"{cb.ACC_TOGGLE}:{account_id}:{page}")
    kb.button(text="Удалить", callback_data=f"{cb.ACC_REMOVE}:{account_id}:{page}")
    kb.adjust(2)

    kb.button(text="← К списку", callback_data=f"{cb.ACC_LIST}:{page}")
    kb.adjust(1)
    return kb


async def _render_accounts_list(q: CallbackQuery, *, page: int) -> None:
    page = max(0, page)

    # Optionally refresh statuses (best-effort) for ACTIVE accounts.
    service = TelethonAccountService(session_storage=DbSessionStorage())

    with SessionLocal() as db:
        accounts_all = list(db.execute(select(Account).order_by(Account.id.asc())).scalars())
        # Hide soft-removed accounts (Remove action): is_active=false + status=forbidden + cleared session.
        accounts_all = [
            a
            for a in accounts_all
            if not (not a.is_active and a.status == AccountStatus.forbidden and (a.session_string or "") == "")
        ]

        if not accounts_all:
            if q.message:
                await q.message.edit_text(
                    "Аккаунты\n\nПока нет аккаунтов.",
                    reply_markup=accounts_actions_kb().as_markup(),
                )
            return

        # Best-effort health refresh for active accounts (so list is truthful).
        for acc in accounts_all:
            if not acc.is_active:
                continue
            try:
                health = await service.check(account_id=acc.id)
                acc.status = health.status
                acc.last_error = health.last_error
                acc.cooldown_until = health.cooldown_until
            except Exception as e:
                acc.status = AccountStatus.error
                acc.last_error = f"{type(e).__name__}: {e}"

        db.commit()

        total_pages = max(1, (len(accounts_all) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, total_pages - 1)
        start = page * PAGE_SIZE
        end = start + PAGE_SIZE
        page_items = accounts_all[start:end]

        lines: list[str] = []
        for acc in page_items:
            active_flag = "active" if acc.is_active else "disabled"
            label = (acc.label or acc.phone_number or f"acc#{acc.id}").strip()
            status = acc.status.value if hasattr(acc.status, "value") else str(acc.status)
            last_error = (acc.last_error or "").strip()
            tail = f"\n    err: {last_error}" if last_error else ""
            hint = ""
            if acc.status == AccountStatus.auth_required:
                hint = "\n    ⚠️ Нужна авторизация (не используется парсером)"
            lines.append(f"#{acc.id} [{active_flag}] {label}\n    status={status}{hint}{tail}")

        prefix = _counts_prefix_accounts(db=db)

    text = "Аккаунты\n\n" + prefix + "\n\n".join(lines)

    if q.message:
        await q.message.edit_text(
            text,
            reply_markup=_accounts_list_kb(accounts=page_items, page=page, total_pages=total_pages).as_markup(),
        )


@router.callback_query(lambda q: (q.data or "") == cb.ACC_LIST or (q.data or "").startswith(f"{cb.ACC_LIST}:"))
async def acc_list(q: CallbackQuery) -> None:
    await q.answer()

    data = (q.data or "").strip()
    page = 0
    if data.startswith(f"{cb.ACC_LIST}:"):
        try:
            # cb.ACC_LIST itself contains ':' (e.g. "accounts:list"), so page is after the LAST ':'
            page = int(data.rsplit(":", 1)[1])
        except Exception:
            page = 0

    await _render_accounts_list(q, page=page)


@router.callback_query(F.data.startswith(f"{cb.ACC_VIEW}:"))
async def acc_view(q: CallbackQuery) -> None:
    await q.answer()

    data = (q.data or "")
    try:
        _, _, account_id_s, page_s = data.split(":", 3)
        account_id = int(account_id_s)
        page = int(page_s)
    except Exception:
        await q.answer("Bad callback", show_alert=False)
        return

    # Refresh single account status (best-effort) to show real info.
    service = TelethonAccountService(session_storage=DbSessionStorage())

    with SessionLocal() as db:
        acc = db.get(Account, account_id)
        if not acc:
            await q.answer("Account not found", show_alert=False)
            return

        if acc.is_active:
            try:
                health = await service.check(account_id=acc.id)
                acc.status = health.status
                acc.last_error = health.last_error
                acc.cooldown_until = health.cooldown_until
            except Exception as e:
                acc.status = AccountStatus.error
                acc.last_error = f"{type(e).__name__}: {e}"

        db.commit()

        label = (acc.label or acc.phone_number or f"acc#{acc.id}").strip()
        active_flag = "active" if acc.is_active else "disabled"
        status = acc.status.value if hasattr(acc.status, "value") else str(acc.status)
        last_error = (acc.last_error or "").strip()
        cooldown = acc.cooldown_until.isoformat() if getattr(acc, "cooldown_until", None) else "—"

    hint = ""
    if acc.status == AccountStatus.auth_required:
        hint = "\n\n⚠️ Нужна авторизация. Этот аккаунт не будет использоваться парсером, пока не переавторизуете." \
            "\nНажмите 'Переавторизовать' ниже и пройдите онбординг заново (код или tdata)."

    text = (
        f"Аккаунт #{account_id}\n\n"
        f"{label}\n"
        f"state={active_flag} status={status}\n"
        f"cooldown_until={cooldown}\n\n"
        f"last_error: {last_error if last_error else '—'}"
        f"{hint}"
    )

    if q.message:
        await q.message.edit_text(
            text,
            reply_markup=_account_detail_kb(
                account_id=account_id,
                is_active=(active_flag == 'active'),
                status=acc.status,
                page=page,
            ).as_markup(),
        )


@router.callback_query(F.data.startswith(f"{cb.ACC_TOGGLE}:"))
async def acc_toggle(q: CallbackQuery) -> None:
    await q.answer()

    data = (q.data or "")
    try:
        _, _, account_id_s, page_s = data.split(":", 3)
        account_id = int(account_id_s)
        page = int(page_s)
    except Exception:
        return

    with SessionLocal() as db:
        acc = db.get(Account, account_id)
        if not acc:
            await q.answer("Account not found", show_alert=False)
            return
        acc.is_active = not acc.is_active
        db.commit()
        new_state = "enabled" if acc.is_active else "disabled"

    await q.answer(f"Account {new_state}")
    await _render_accounts_list(q, page=page)


@router.callback_query(F.data.startswith(f"{cb.ACC_REMOVE}:"))
async def acc_remove(q: CallbackQuery) -> None:
    """Soft-remove an account.

    We intentionally DO NOT hard-delete accounts because they can be referenced by
    membership/history rows (and hard delete may fail or break invariants).

    DoD for "remove":
    - account is deactivated (is_active=False)
    - status set to a non-usable state
    - session_string wiped
    - UI list refreshed (same page)
    """

    await q.answer()

    data = (q.data or "")
    try:
        _, _, account_id_s, page_s = data.split(":", 3)
        account_id = int(account_id_s)
        page = int(page_s)
    except Exception:
        return

    with SessionLocal() as db:
        acc = db.get(Account, account_id)
        if not acc:
            await q.answer("Account not found", show_alert=False)
            return

        # soft remove
        acc.is_active = False
        # We don't have AccountStatus.removed; 'forbidden' is used as a safe quarantined state.
        acc.status = AccountStatus.forbidden
        acc.session_string = ""
        acc.cooldown_until = None
        if not (acc.last_error or "").strip():
            acc.last_error = "removed by operator"

        # Mark related memberships as forbidden too (best-effort; keeps UI consistent).
        memberships = list(
            db.execute(select(AccountChannelMembership).where(AccountChannelMembership.account_id == account_id))
            .scalars()
            .all()
        )
        for m in memberships:
            m.status = AccountChannelStatus.forbidden
            if not (m.note or "").strip():
                m.note = "account removed"

        db.commit()

    await q.answer(f"Account #{account_id} removed")
    await _render_accounts_list(q, page=page)
