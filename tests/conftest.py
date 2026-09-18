"""Fixtures that build a Maildir by hand.

Nothing here touches a real archive: every awkward case is constructed, so
the tests state exactly which malformation they are about.
"""

from __future__ import annotations

import argparse
from email.message import EmailMessage
from pathlib import Path

import pytest

# A one-pixel transparent GIF -- the single most duplicated thing in real mail.
PIXEL = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
    b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D"
    b"\x01\x00;"
)
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def write_message(folder: Path, name: str, msg: EmailMessage | bytes) -> Path:
    """Drop a message into a maildir folder's cur/."""
    cur = folder / "cur"
    cur.mkdir(parents=True, exist_ok=True)
    (folder / "new").mkdir(exist_ok=True)
    (folder / "tmp").mkdir(exist_ok=True)
    path = cur / name
    path.write_bytes(bytes(msg) if isinstance(msg, EmailMessage) else msg)
    return path


def simple(
    subject="Hello",
    sender="them@example.org",
    to="me@example.com",
    date="Tue, 12 Mar 2013 09:15:00 +0100",
) -> EmailMessage:
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = sender
    m["To"] = to
    m["Date"] = date
    m.set_content("Body text.")
    return m


@pytest.fixture
def maildir(tmp_path: Path) -> Path:
    """A small Maildir++ tree covering the cases that matter."""
    root = tmp_path / "Maildir"

    # Inbox: one message, one PNG attachment.
    m = simple()
    m.add_attachment(PNG, maintype="image", subtype="png", filename="logo.png")
    write_message(root, "1362000000.a,S=1:2,S", m)

    # Sent (German name) with the pixel. From is an old address of ours that
    # --me does not cover and To is the other party, so neither address says
    # anything and only the folder name can tell us it is outgoing. This is the
    # common shape in a long-lived mailbox whose owner changed provider.
    m = simple(sender="old-address@elsewhere.net", to="someone@elsewhere.org")
    m.add_attachment(PIXEL, maintype="image", subtype="gif", filename="spacer.gif")
    write_message(root / ".Gesendet", "1362000001.b,S=1:2,S", m)

    # The same pixel again elsewhere: dedup must collapse these to one blob.
    m = simple(subject="Another")
    m.add_attachment(PIXEL, maintype="image", subtype="gif", filename="tracker.gif")
    write_message(root / ".Archives.2013", "1362000002.c,S=1:2,S", m)

    # A filename that must never become a path.
    m = simple(subject="Nasty name")
    m.add_attachment(
        PDF, maintype="application", subtype="pdf", filename="../../etc/passwd.pdf"
    )
    write_message(root / ".Archives.2013", "1362000003.d,S=1:2,S", m)

    # tmp/ must be ignored entirely.
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    (root / "tmp" / "half-written").write_bytes(b"From: x\n\nnot a real message")

    return root


@pytest.fixture
def extracted(maildir: Path, tmp_path: Path) -> Path:
    """Run a real extraction over the handmade maildir."""
    from attic import extract

    out = tmp_path / "out"
    extract.run(
        argparse.Namespace(
            maildir=maildir, out=out, me=["@example.com"], min_size=1, limit=0, reparse=False
        )
    )
    return out
