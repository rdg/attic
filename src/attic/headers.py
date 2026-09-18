"""Reading headers off mail that is up to twenty-five years old.

Every function here tolerates malformed input rather than raising: the
archive is the input and it cannot be corrected, so a header that will not
decode yields a best effort and a recorded fallback.
"""

from __future__ import annotations

import re
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path


def decode_hdr(raw: str | None) -> str | None:
    """Decode an RFC 2047 header, tolerating the many ways old mail breaks it."""
    if raw is None:
        return None
    try:
        return str(make_header(decode_header(raw))).strip() or None
    except Exception:
        return raw.strip() or None


def clean(text: str | None) -> str | None:
    """Strip NULs and collapse whitespace; sqlite dislikes embedded NULs."""
    if text is None:
        return None
    text = text.replace("\x00", "").strip()
    text = re.sub(r"\s+", " ", text)
    return text or None


# Fallback only -- direction is decided by the addresses first. Needed because
# folder naming is per-mailbox and per-language: .Gesendet, .sent-mail and
# .Sent Messages all mean the same thing. Word-bounded so "consent" is not a hit.
SENT_FOLDER = re.compile(r"\b(sent|gesendet|ausgang|outbox|postausgang)\b", re.I)

EPOCH_PREFIX = re.compile(r"^(\d{9,11})\.")
FOLDER_YEAR = re.compile(r"\.(19[89]\d|20[0-4]\d)(?:\.|$)")


def folder_of(root: Path, path: Path) -> str:
    """The maildir folder a message lives in: '.Archives.2015.Inbox' or 'INBOX'."""
    rel = path.relative_to(root)
    parts = rel.parts
    # <root>/[.folder.name/]{cur,new}/<file>
    if len(parts) >= 3:
        return parts[0]
    return "INBOX"


def first_addr(msg, header: str) -> str | None:
    try:
        raw = msg.get_all(header, [])
        pairs = getaddresses([str(v) for v in raw])
        for _name, addr in pairs:
            if addr and "@" in addr:
                return addr.lower()
    except Exception:
        pass
    return None


def all_addrs(msg, headers: tuple[str, ...]) -> set[str]:
    out: set[str] = set()
    for header in headers:
        try:
            for _name, addr in getaddresses([str(v) for v in msg.get_all(header, [])]):
                if addr and "@" in addr:
                    out.add(addr.lower())
        except Exception:
            continue
    return out


def when(msg, path: Path, folder: str) -> tuple[str | None, str | None, str]:
    """Best-effort date: the header, then the maildir filename, then the folder year."""
    raw = None
    try:
        raw = clean(str(msg.get("Date"))) if msg.get("Date") else None
    except Exception:
        raw = None
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            if dt is not None and 1980 < dt.year < 2100:
                return raw, dt.isoformat(), "header"
        except Exception:
            pass
    match = EPOCH_PREFIX.match(path.name)
    if match:
        try:
            import datetime as _dt

            dt = _dt.datetime.fromtimestamp(int(match.group(1)), _dt.UTC)
            if 1980 < dt.year < 2100:
                return raw, dt.isoformat(), "filename"
        except Exception:
            pass
    match = FOLDER_YEAR.search(folder)
    if match:
        return raw, f"{match.group(1)}-01-01T00:00:00+00:00", "folder"
    return raw, None, "none"
