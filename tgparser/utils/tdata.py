from __future__ import annotations

import os
import shutil
import zipfile


class TdataArchiveError(RuntimeError):
    pass


def _safe_extract_zip(*, z: zipfile.ZipFile, dst_dir: str) -> None:
    """Extract zip contents into dst_dir preventing Zip Slip path traversal."""

    dst_dir_abs = os.path.abspath(dst_dir)

    for member in z.infolist():
        # Skip directory entries; they'll be created implicitly.
        member_name = member.filename
        if not member_name:
            continue

        # Zip files always use forward slashes.
        # Disallow absolute paths and path traversal.
        normalized = os.path.normpath(member_name).lstrip("/\\")
        if normalized.startswith(".." + os.sep) or normalized == "..":
            raise TdataArchiveError("unsafe archive paths detected")

        out_path = os.path.abspath(os.path.join(dst_dir_abs, normalized))
        if not out_path.startswith(dst_dir_abs + os.sep) and out_path != dst_dir_abs:
            raise TdataArchiveError("unsafe archive paths detected")

        # Ensure parent dir exists.
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        if member.is_dir():
            os.makedirs(out_path, exist_ok=True)
            continue

        with z.open(member) as src, open(out_path, "wb") as dst:
            shutil.copyfileobj(src, dst)


def extract_tdata_from_archive(*, archive_path: str, extract_root: str) -> str:
    """Extract a user-uploaded archive and return path to the extracted 'tdata' folder.

    Security: extracted files may contain live Telegram session material.
    Caller is responsible for removing extract_root after onboarding.

    Expected layouts:
    - tdata/... (at root)
    - <something>/tdata/...
    """

    if not os.path.exists(archive_path):
        raise TdataArchiveError("archive not found")

    if not zipfile.is_zipfile(archive_path):
        raise TdataArchiveError("unsupported archive (expected .zip)")

    os.makedirs(extract_root, exist_ok=True)

    with zipfile.ZipFile(archive_path) as z:
        _safe_extract_zip(z=z, dst_dir=extract_root)

    # Find tdata folder
    candidate = os.path.join(extract_root, "tdata")
    if os.path.isdir(candidate):
        tdata_dir = candidate
    else:
        tdata_dir = ""
        for root, dirs, _files in os.walk(extract_root):
            if "tdata" in dirs:
                tdata_dir = os.path.join(root, "tdata")
                break

        if not tdata_dir:
            raise TdataArchiveError("tdata folder not found in archive")

    # Basic sanity: not empty (common user mistake: zip wrong folder level)
    try:
        entries = os.listdir(tdata_dir)
    except FileNotFoundError:
        raise TdataArchiveError("tdata folder not found in archive")

    if not entries:
        raise TdataArchiveError(
            "tdata folder is empty. Please zip Telegram Desktop 'tdata' folder (with files inside) and retry."
        )

    return tdata_dir
