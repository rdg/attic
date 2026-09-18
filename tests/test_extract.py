"""End-to-end extraction against a handmade Maildir."""

from __future__ import annotations

import argparse
import sqlite3

import pytest

from attic import extract


def rows(out, sql, *args):
    db = sqlite3.connect(f"file:{out / 'index.db'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(sql, args)]
    finally:
        db.close()


def test_every_message_is_indexed(extracted):
    assert rows(extracted, "SELECT count(*) n FROM messages")[0]["n"] == 4


def test_tmp_directory_is_ignored(extracted):
    """Half-written mail in a maildir's tmp/ is not a message.

    Checks the parent directory rather than the substring "/tmp/": on Linux
    pytest's own tmp_path lives under /tmp, so a substring test passes
    everywhere for the wrong reason.
    """
    from pathlib import Path as P

    paths = [r["p"] for r in rows(extracted, "SELECT maildir_path p FROM messages")]
    assert paths, "the fixture should have indexed something"
    assert all(P(p).parent.name in ("cur", "new") for p in paths)
    assert not any("half-written" in p for p in paths)


def test_identical_bytes_are_stored_once(extracted):
    """The same pixel in two messages is one blob and two occurrences."""
    gif = rows(
        extracted, "SELECT sha256, count(*) n FROM attachments GROUP BY sha256 HAVING n > 1"
    )
    assert len(gif) == 1 and gif[0]["n"] == 2
    assert rows(extracted, "SELECT count(*) n FROM blobs")[0]["n"] == 3


def test_blobs_are_content_addressed_on_disk(extracted):
    for b in rows(extracted, "SELECT sha256, blob_path FROM blobs"):
        path = extracted / "blobs" / b["blob_path"]
        assert path.exists()
        import hashlib

        assert hashlib.sha256(path.read_bytes()).hexdigest() == b["sha256"]


def test_hostile_filename_never_becomes_a_path(extracted):
    """'../../etc/passwd.pdf' is recorded, not written."""
    names = [r["filename"] for r in rows(extracted, "SELECT filename FROM attachments")]
    assert "../../etc/passwd.pdf" in names
    assert not (extracted / "blobs" / ".." / ".." / "etc").exists()
    written = [p.name for p in (extracted / "blobs").rglob("*") if p.is_file()]
    assert all(len(n) == 64 for n in written), written


def test_type_is_sniffed_not_trusted(extracted):
    png = rows(extracted, "SELECT sniffed_type FROM v_attachments WHERE filename='logo.png'")
    assert png[0]["sniffed_type"] == "image/png"


def test_direction_from_addresses(extracted):
    inbox = rows(extracted, "SELECT direction FROM messages WHERE folder='INBOX'")
    assert inbox[0]["direction"] == "in"


def test_direction_from_a_german_sent_folder(extracted):
    """Neither address is --me here, so only the folder name can decide."""
    sent = rows(extracted, "SELECT direction FROM messages WHERE folder='.Gesendet'")
    assert sent[0]["direction"] == "out"


def test_being_a_recipient_beats_the_folder_name(maildir, tmp_path):
    """Documents a deliberate precedence.

    A message addressed to you, from someone else, is incoming even when it
    sits in a folder called Sent -- an archive folder named 'sent-mail' may
    well hold received mail, whereas being in the To: line is direct evidence.
    Addresses outrank folder names; only when no address matches does the
    folder get a say.
    """
    from email.message import EmailMessage

    from .conftest import PNG, write_message

    m = EmailMessage()
    m["Subject"] = "filed oddly"
    m["From"] = "someone@elsewhere.org"
    m["To"] = "me@example.com"
    m["Date"] = "Tue, 12 Mar 2013 09:15:00 +0100"
    m.set_content("x")
    m.add_attachment(PNG, maintype="image", subtype="png", filename="z.png")
    write_message(maildir / ".Sent Messages", "1362000009.z,S=1:2,S", m)

    out = tmp_path / "precedence"
    extract.run(
        argparse.Namespace(
            maildir=maildir, out=out, me=["@example.com"], min_size=1, limit=0, reparse=False
        )
    )
    got = rows(out, "SELECT direction FROM messages WHERE folder='.Sent Messages'")
    assert got[0]["direction"] == "in"


def test_rerun_skips_everything(maildir, extracted):
    """Resumability: the claim that a re-run reads only new mail."""
    before = rows(extracted, "SELECT count(*) n FROM attachments")[0]["n"]
    extract.run(
        argparse.Namespace(
            maildir=maildir,
            out=extracted,
            me=["@example.com"],
            min_size=1,
            limit=0,
            reparse=False,
        )
    )
    after = rows(extracted, "SELECT count(*) n FROM attachments")[0]["n"]
    assert after == before, "a re-run must not double-insert"


def test_reparse_is_idempotent(maildir, extracted):
    before = rows(extracted, "SELECT count(*) n FROM attachments")[0]["n"]
    extract.run(
        argparse.Namespace(
            maildir=maildir,
            out=extracted,
            me=["@example.com"],
            min_size=1,
            limit=0,
            reparse=True,
        )
    )
    after = rows(extracted, "SELECT count(*) n FROM attachments")[0]["n"]
    assert after == before


def test_min_size_filters_parts(maildir, tmp_path):
    """50 bytes keeps the 67-byte PNG and drops the 43-byte tracking pixel."""
    out = tmp_path / "big-only"
    extract.run(
        argparse.Namespace(
            maildir=maildir, out=out, me=["@example.com"], min_size=50, limit=0, reparse=False
        )
    )
    sizes = [r["size"] for r in rows(out, "SELECT size FROM blobs")]
    assert sizes and all(s >= 50 for s in sizes)
    assert 43 not in sizes, "the pixel should have been filtered out"


def test_missing_maildir_raises_a_clean_error(tmp_path):
    """A library raises; turning that into an exit code is the CLI's job."""
    from attic.errors import AtticError

    with pytest.raises(AtticError, match="not a maildir"):
        extract.run(
            argparse.Namespace(
                maildir=tmp_path / "nope",
                out=tmp_path / "o",
                me=[],
                min_size=1,
                limit=0,
                reparse=False,
            )
        )


def test_no_me_means_no_address_is_mine(maildir, tmp_path):
    """Without --me the tool must not guess a domain and mislabel everything."""
    out = tmp_path / "nome"
    extract.run(
        argparse.Namespace(maildir=maildir, out=out, me=[], min_size=1, limit=0, reparse=False)
    )
    dirs = {
        r["folder"]: r["direction"] for r in rows(out, "SELECT folder, direction FROM messages")
    }
    assert dirs["INBOX"] == "in"
    assert dirs[".Gesendet"] == "out", "folder name is the only signal left"
