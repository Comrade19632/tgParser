from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ...db import SessionLocal
from ...models import BotUser
from ...settings import settings

router = Router()


def _is_admin_chat(m: Message) -> bool:
    admin_chat_id = getattr(settings, "admin_chat_id", None)
    if not admin_chat_id or not m.chat:
        return False
    try:
        return int(m.chat.id) == int(admin_chat_id)
    except Exception:
        return False


def _is_staff_user(telegram_user_id: int) -> bool:
    with SessionLocal() as db:
        u = db.query(BotUser).filter(BotUser.telegram_user_id == int(telegram_user_id)).one_or_none()
        return bool(u and u.is_staff)


def _require_admin_or_staff(m: Message) -> bool:
    if _is_admin_chat(m):
        return True
    if m.from_user and _is_staff_user(m.from_user.id):
        return True
    return False


@router.message(Command("whoami"))
async def cmd_whoami(m: Message) -> None:
    if not m.from_user:
        return
    await m.answer(
        "IDs:\n"
        f"- chat_id: {m.chat.id if m.chat else '?'}\n"
        f"- user_id: {m.from_user.id}"
    )


@router.message(Command("staff_list"))
async def cmd_staff_list(m: Message) -> None:
    if not _require_admin_or_staff(m):
        await m.answer("Доступ запрещен")
        return

    with SessionLocal() as db:
        rows = (
            db.query(BotUser.telegram_user_id, BotUser.notify_enabled)
            .filter(BotUser.is_staff.is_(True))
            .order_by(BotUser.telegram_user_id.asc())
            .all()
        )

    if not rows:
        await m.answer("Staff list пуст")
        return

    lines = ["Staff users:"]
    for uid, notify_enabled in rows:
        lines.append(f"- {int(uid)} (notify={'on' if notify_enabled else 'off'})")
    await m.answer("\n".join(lines))


@router.message(Command("staff_add"))
async def cmd_staff_add(m: Message) -> None:
    if not _require_admin_or_staff(m):
        await m.answer("Доступ запрещен")
        return

    parts = (m.text or "").strip().split()
    if len(parts) < 2:
        await m.answer("Usage: /staff_add <telegram_user_id>")
        return

    try:
        target_id = int(parts[1])
    except Exception:
        await m.answer("Bad telegram_user_id")
        return

    with SessionLocal() as db:
        u = db.query(BotUser).filter(BotUser.telegram_user_id == target_id).one_or_none()
        if not u:
            u = BotUser(telegram_user_id=target_id, is_staff=True)
            db.add(u)
        else:
            u.is_staff = True
        db.commit()

    await m.answer(f"OK: {target_id} is_staff=true")


@router.message(Command("staff_remove"))
async def cmd_staff_remove(m: Message) -> None:
    if not _require_admin_or_staff(m):
        await m.answer("Доступ запрещен")
        return

    parts = (m.text or "").strip().split()
    if len(parts) < 2:
        await m.answer("Usage: /staff_remove <telegram_user_id>")
        return

    try:
        target_id = int(parts[1])
    except Exception:
        await m.answer("Bad telegram_user_id")
        return

    with SessionLocal() as db:
        u = db.query(BotUser).filter(BotUser.telegram_user_id == target_id).one_or_none()
        if not u:
            await m.answer("Not found")
            return
        u.is_staff = False
        db.commit()

    await m.answer(f"OK: {target_id} is_staff=false")
