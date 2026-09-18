"""Turning mail text into something readable.

Two jobs. Stripping markup, because an HTML part must never reach the
page as markup -- mail carries tracking pixels and worse. And guessing a
character set, because mail predating universal UTF-8 is mostly cp1252 and
decoding it as UTF-8 turns every umlaut into a replacement character.
"""

from __future__ import annotations

import html as html_mod
import re

# Tried in order. cp1252 before latin-1 because it is a superset for the
# printable range and gets curly quotes and dashes right.
ENCODINGS = ("utf-8", "cp1252", "latin-1")


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """Decode text of unknown vintage. Returns (text, encoding used)."""
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace"), "latin-1"


TAGS = re.compile(r"<[^>]+>")
DROP = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
BREAKS = re.compile(r"<\s*(br|/p|/div|/tr|/h[1-6])\b[^>]*>", re.I)


def html_to_text(raw: str) -> str:
    """Crude but safe: markup never reaches the browser, only its text."""
    raw = DROP.sub(" ", raw)
    raw = BREAKS.sub("\n", raw)
    raw = TAGS.sub("", raw)
    raw = html_mod.unescape(raw)
    return re.sub(r"\n{3,}", "\n\n", raw).strip()


def extract_body(msg) -> str:
    """Prefer a text/plain part; fall back to the text of an HTML one."""
    plain, rich = [], []
    try:
        walker = msg.walk()
    except Exception:
        return ""
    for part in walker:
        ctype = (part.get_content_type() or "").lower()
        if ctype not in ("text/plain", "text/html"):
            continue
        if (part.get_content_disposition() or "").lower() == "attachment":
            continue
        try:
            data = part.get_payload(decode=True)
            if data is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = data.decode(charset, "replace")
        except Exception:
            continue
        (plain if ctype == "text/plain" else rich).append(text)
    if plain:
        return "\n\n".join(plain).strip()
    if rich:
        return html_to_text("\n\n".join(rich))
    return "(no text body -- this message was attachments only)"
