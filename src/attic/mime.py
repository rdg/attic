"""Walking a message's MIME tree and pulling parts out of it.

Separated from extraction so the awkward cases -- an attached message
carrying its own attachments, a part whose transfer encoding is broken --
can be tested against handmade messages rather than a real archive.
"""

from __future__ import annotations

import email
import email.policy


def is_attachment(part) -> bool:
    """A part worth extracting: declared as one, named, or simply not body text."""
    ctype = (part.get_content_type() or "").lower()
    if ctype.startswith("multipart/"):
        return False
    disp = (part.get_content_disposition() or "").lower()
    if disp == "attachment":
        return True
    if part.get_filename():
        return True
    if part.get("Content-ID"):
        return True
    # Body text and the envelope of an attached message are not themselves
    # attachments; the attached message's own parts are reached by the walker.
    return not (ctype.startswith("text/") or ctype == "message/rfc822")


def payload_bytes(part) -> tuple[bytes | None, bool]:
    """Decoded bytes, or the raw payload flagged when the transfer encoding is broken."""
    try:
        data = part.get_payload(decode=True)
    except Exception:
        data = None
    if data is not None:
        return data, False
    try:
        raw = part.get_payload()
    except Exception:
        return None, True
    if isinstance(raw, str):
        return raw.encode("utf-8", "replace"), True
    if isinstance(raw, bytes):
        return raw, True
    return None, True


def walk_parts(msg, prefix: str = ""):
    """Yield (part_path, part) depth-first, descending into attached messages."""
    if msg.is_multipart():
        payload = msg.get_payload()
        if not isinstance(payload, list):
            return
        for index, sub in enumerate(payload, start=1):
            path = f"{prefix}{index}"
            ctype = (sub.get_content_type() or "").lower()
            if ctype == "message/rfc822":
                yield path, sub
                inner = sub.get_payload()
                inner = inner[0] if isinstance(inner, list) and inner else inner
                if hasattr(inner, "walk"):
                    yield from walk_parts(inner, prefix=f"{path}.")
            elif sub.is_multipart():
                yield from walk_parts(sub, prefix=f"{path}.")
            else:
                yield path, sub
    else:
        yield prefix or "1", msg


def parse_message(raw: bytes):
    """Parse with the modern policy, falling back to compat32 on malformed headers."""
    try:
        return email.message_from_bytes(raw, policy=email.policy.default), None
    except Exception as exc:
        try:
            return email.message_from_bytes(raw), f"compat32 fallback: {exc!r}"
        except Exception as exc2:
            return None, f"unparseable: {exc2!r}"
