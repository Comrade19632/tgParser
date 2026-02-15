from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from telethon import errors
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from ..models import Channel, ChannelAccessStatus, ChannelType

log = logging.getLogger(__name__)


_INVITE_RE = re.compile(r"(?:https?://)?t\.me/\+(?P<hash>[A-Za-z0-9_-]+)")


@dataclass(frozen=True)
class EnsureJoinedResult:
    ok: bool
    # Best-effort Telethon entity from dialogs/join result.
    entity: object | None = None
    # Updated access status after the attempt.
    access_status: ChannelAccessStatus | None = None
    # User-facing short note (safe to show in operator notifications).
    note: str = ""


def _extract_invite_hash(invite_link_or_hash: str) -> str:
    raw = (invite_link_or_hash or "").strip()
    if not raw:
        return ""

    # Allow passing bare hash.
    if "/" not in raw and raw[0] not in {"+"} and "t.me" not in raw:
        return raw

    m = _INVITE_RE.search(raw)
    if m:
        return m.group("hash")

    # Fallback: t.me/+HASH but without scheme.
    raw = raw.replace("https://", "").replace("http://", "")
    raw = raw.lstrip("/")
    if raw.startswith("t.me/+" ):
        return raw.split("t.me/+", 1)[-1].split("/", 1)[0]

    return ""


async def ensure_joined(*, client, ch: Channel, force: bool = False) -> EnsureJoinedResult:
    """Ensure channel membership.

    Goal: reduce expensive resolve/get_entity calls by joining once and then relying on dialogs.

    DB mutations are NOT done here (service is pure). Caller updates ch fields and commits.
    """

    now = datetime.now(timezone.utc)

    # NOTE: channel.access_status is global, while membership is per-account.
    # For *private* channels we must be able to call ImportChatInviteRequest even if the
    # channel was previously marked active/joined by some other account.
    # For *public* channels, a joined/active status is sufficient to skip re-joining.
    if (
        (not force)
        and ch.type == ChannelType.public
        and ch.access_status in {ChannelAccessStatus.joined, ChannelAccessStatus.active}
    ):
        return EnsureJoinedResult(ok=True, entity=None, access_status=ch.access_status)

    try:
        if ch.type == ChannelType.public:
            # get_entity(@username) is acceptable for public; later parsing should use dialogs.
            ref = (ch.identifier or "").strip()
            if not ref:
                return EnsureJoinedResult(
                    ok=False,
                    access_status=ChannelAccessStatus.error,
                    note="empty public channel identifier",
                )

            if not ref.startswith("@") and "t.me/" not in ref:
                ref = "@" + ref.lstrip("@").strip()

            entity = await client.get_entity(ref)
            try:
                await client(JoinChannelRequest(entity))
            except errors.UserAlreadyParticipantError:
                pass

            # Best-effort: return entity (may still not be in dialogs cache yet).
            return EnsureJoinedResult(
                ok=True,
                entity=entity,
                access_status=ChannelAccessStatus.joined,
                note="joined public channel",
            )

        # private
        invite_hash = _extract_invite_hash(ch.identifier)
        if not invite_hash:
            return EnsureJoinedResult(
                ok=False,
                access_status=ChannelAccessStatus.error,
                note="invalid invite link/hash",
            )

        try:
            res = await client(ImportChatInviteRequest(invite_hash))
        except errors.UserAlreadyParticipantError:
            return EnsureJoinedResult(
                ok=True,
                entity=None,
                access_status=ChannelAccessStatus.joined,
                note="already participant",
            )
        except getattr(errors, "InviteRequestSentError", ()):  # join request sent, pending approval
            # Telegram indicates the request was created; we should not spam ImportChatInvite.
            return EnsureJoinedResult(
                ok=False,
                entity=None,
                access_status=ChannelAccessStatus.join_requested,
                note="join request sent (pending approval)",
            )

        # ImportChatInviteRequest returns Updates; chats may include the joined channel.
        joined_entity = None
        chats = getattr(res, "chats", None)
        if chats:
            joined_entity = chats[0]

        return EnsureJoinedResult(
            ok=True,
            entity=joined_entity,
            access_status=ChannelAccessStatus.joined,
            note="imported private invite",
        )

    except errors.ChatAdminRequiredError as e:
        log.info("ensure_joined forbidden: %s", e)
        return EnsureJoinedResult(ok=False, access_status=ChannelAccessStatus.forbidden, note="forbidden")
    except errors.FloodWaitError as e:
        return EnsureJoinedResult(ok=False, access_status=ChannelAccessStatus.error, note=f"FloodWait {e.seconds}s")
    except errors.RPCError as e:
        return EnsureJoinedResult(ok=False, access_status=ChannelAccessStatus.error, note=f"RPCError: {type(e).__name__}")
    except Exception as e:
        return EnsureJoinedResult(ok=False, access_status=ChannelAccessStatus.error, note=f"error: {type(e).__name__}")
