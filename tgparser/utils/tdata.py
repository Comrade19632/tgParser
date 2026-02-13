from __future__ import annotations

import os
import shutil
import tempfile
import zipfile


class TdataArchiveError(RuntimeError):
    pass


def extract_tdata_from_archive(*, archive_path: str) -> str:
    """Extract a user-uploaded archive and return path to the extracted 'tdata' folder.

    Supports .zip archives created by Telegram Desktop export/backups.

    Expected layouts:
    - tdata/... (at root)
    - <something>/tdata/...
    """

    if not os.path.exists(archive_path):
        raise TdataArchiveError("archive not found")

    tmp_root = tempfile.mkdtemp(prefix="tgparser_tdata_")

    try:
        if not zipfile.is_zipfile(archive_path):
            raise TdataArchiveError("unsupported archive (expected .zip)")

        with zipfile.ZipFile(archive_path) as z:
            z.extractall(tmp_root)

        # Find tdata folder
        candidate = os.path.join(tmp_root, "tdata")
        if os.path.isdir(candidate):
            return candidate

        for root, dirs, _files in os.walk(tmp_root):
            if "tdata" in dirs:
                return os.path.join(root, "tdata")

        raise TdataArchiveError("tdata folder not found in archive")
    except Exception:
        # Keep tmp_root for debugging? No, this can hold secrets; remove.
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise
