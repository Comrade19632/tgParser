from __future__ import annotations

import logging

from ..models import Channel, ChannelType

log = logging.getLogger(__name__)


def _norm_username(identifier: str) -> str:
    raw = (identifier or "").strip()
    if not raw:
        return ""
    if "t.me/" in raw:
        raw = raw.split("t.me/", 1)[-1]
        raw = raw.split("/", 1)[0]
    return raw.lstrip("@").strip().lower()


async def get_entity_from_dialogs(*, client, ch: Channel, limit: int = 200):
    """Find channel entity via dialogs.

    This avoids resolve username / extra API calls once membership exists.
    """

    dialogs = await client.get_dialogs(limit=limit)

    if ch.type == ChannelType.public:
        username = _norm_username(ch.identifier)
        if not username:
            return None
        for d in dialogs:
            ent = getattr(d, "entity", None)
            u = (getattr(ent, "username", None) or "").strip().lower()
            if u and u == username:
                return ent
        return None

    # private: best-effort by numeric id if present on the object.
    peer_id = getattr(ch, "peer_id", None)
    if isinstance(peer_id, int) and peer_id:
        for d in dialogs:
            ent = getattr(d, "entity", None)
            ent_id = getattr(ent, "id", None)
            if isinstance(ent_id, int) and ent_id == peer_id:
                return ent

    return None
