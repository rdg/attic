"""Walk a Maildir tree, extract every attachment, index it in sqlite.

Blobs are content-addressed, so a logo appearing in four thousand signatures
is stored once and recorded four thousand times. Declared filenames are kept
in the database and never used as a path on disk -- decades of mail contain
filenames with slashes, colons, NULs and RFC 2231 wreckage.

The run is resumable: a message whose path, mtime and size already match a
row is skipped, so a re-run after a fresh sync reads only new mail.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from pathlib import Path

from .errors import AtticError
from .headers import (
    SENT_FOLDER,
    all_addrs,
    clean,
    decode_hdr,
    first_addr,
    folder_of,
    when,
)
from .mime import is_attachment, parse_message, payload_bytes, walk_parts
from .sniff import sniff

SCHEMA = Path(__file__).parent / "schema.sql"


def find_messages(root: Path):
    """Every file under a cur/ or new/ directory. tmp/ is ignored by design."""
    for dirpath, dirnames, filenames in os.walk(root):
        base = os.path.basename(dirpath)
        if base == "tmp":
            dirnames[:] = []
            continue
        if base not in ("cur", "new"):
            continue
        for name in filenames:
            if name.startswith("."):
                continue
            yield Path(dirpath) / name


def run(args) -> int:
    """Walk the maildir and index it. Arguments come from the CLI."""

    maildir = args.maildir.expanduser().resolve()
    out = args.out.expanduser().resolve()
    if not maildir.is_dir():
        raise AtticError(f"not a maildir directory: {maildir}")

    # No default: with no --me, no address is "mine", so direction falls to
    # the folder name. Guessing a domain here would silently mislabel
    # somebody else's archive.
    me = [m.lower() for m in (args.me or [])]
    blob_root = out / "blobs"
    blob_root.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(out / "index.db")
    db.executescript(SCHEMA.read_text())

    known: dict[str, tuple[float, int]] = {}
    if not args.reparse:
        for path, mtime, size in db.execute("SELECT maildir_path, mtime, size FROM messages"):
            known[path] = (mtime, size)

    seen_blobs = {row[0] for row in db.execute("SELECT sha256 FROM blobs")}

    stats = {
        "scanned": 0,
        "indexed": 0,
        "skipped": 0,
        "attachments": 0,
        "new_blobs": 0,
        "dup_blobs": 0,
        "errors": 0,
    }

    def is_mine(addr: str | None) -> bool:
        if not addr:
            return False
        return any(addr == m or addr.endswith(m) for m in me)

    for path in find_messages(maildir):
        stats["scanned"] += 1
        if stats["scanned"] % 2000 == 0:
            print(
                f"  ...{stats['scanned']} scanned, {stats['indexed']} indexed, "
                f"{stats['attachments']} attachments, {stats['errors']} errors",
                file=sys.stderr,
                flush=True,
            )

        key = str(path)
        try:
            st = path.stat()
        except OSError:
            stats["errors"] += 1
            continue

        if not args.reparse and key in known and known[key] == (st.st_mtime, st.st_size):
            stats["skipped"] += 1
            continue

        folder = folder_of(maildir, path)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            stats["errors"] += 1
            print(f"read failed {path}: {exc}", file=sys.stderr)
            continue

        msg, parse_error = parse_message(raw)
        if msg is None:
            stats["errors"] += 1
            db.execute(
                "INSERT OR REPLACE INTO messages "
                "(maildir_path, folder, mtime, size, parse_error, direction, date_source) "
                "VALUES (?,?,?,?,?,'unknown','none')",
                (key, folder, st.st_mtime, st.st_size, parse_error),
            )
            continue

        try:
            from_addr = first_addr(msg, "From")
            date_raw, date_parsed, date_source = when(msg, path, folder)
            # Direction from the addresses, with the folder name as a fallback.
            # Stored, not assumed -- an UPDATE re-derives it without a re-scan.
            recipients = all_addrs(msg, ("To", "Cc", "Bcc", "Delivered-To"))
            if is_mine(from_addr):
                direction = "out"
            elif any(is_mine(a) for a in recipients):
                direction = "in"
            elif SENT_FOLDER.search(folder.rsplit(".", 1)[-1]):
                direction = "out"
            elif from_addr:
                direction = "in"
            else:
                direction = "unknown"

            row = db.execute(
                "INSERT INTO messages (maildir_path, folder, mtime, size, message_id,"
                " from_addr, from_raw, to_raw, cc_raw, subject, date_raw, date_parsed,"
                " date_source, direction, parse_error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(maildir_path) DO UPDATE SET"
                "  mtime=excluded.mtime, size=excluded.size, subject=excluded.subject,"
                "  direction=excluded.direction, date_parsed=excluded.date_parsed"
                " RETURNING id",
                (
                    key,
                    folder,
                    st.st_mtime,
                    st.st_size,
                    clean(
                        decode_hdr(
                            str(msg.get("Message-ID")) if msg.get("Message-ID") else None
                        )
                    ),
                    from_addr,
                    clean(decode_hdr(str(msg.get("From")) if msg.get("From") else None)),
                    clean(decode_hdr(str(msg.get("To")) if msg.get("To") else None)),
                    clean(decode_hdr(str(msg.get("Cc")) if msg.get("Cc") else None)),
                    clean(decode_hdr(str(msg.get("Subject")) if msg.get("Subject") else None)),
                    date_raw,
                    date_parsed,
                    date_source,
                    direction,
                    parse_error,
                ),
            ).fetchone()
            msg_id = row[0]
            db.execute("DELETE FROM attachments WHERE message_id_fk = ?", (msg_id,))
            stats["indexed"] += 1
        except Exception as exc:
            stats["errors"] += 1
            print(f"index failed {path}: {exc}", file=sys.stderr)
            continue

        for part_path, part in walk_parts(msg):
            try:
                if not is_attachment(part):
                    continue
                data, decode_failed = payload_bytes(part)
                if data is None or len(data) < args.min_size:
                    continue

                sha = hashlib.sha256(data).hexdigest()
                if sha not in seen_blobs:
                    mime_sniffed, ext = sniff(data)
                    dest = blob_root / sha[:2] / sha
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if not dest.exists():
                        tmp = dest.with_suffix(".part")
                        tmp.write_bytes(data)
                        tmp.rename(dest)
                    db.execute(
                        "INSERT OR IGNORE INTO blobs (sha256, size, blob_path,"
                        " sniffed_type, sniffed_ext) VALUES (?,?,?,?,?)",
                        (sha, len(data), f"{sha[:2]}/{sha}", mime_sniffed, ext),
                    )
                    seen_blobs.add(sha)
                    stats["new_blobs"] += 1
                else:
                    stats["dup_blobs"] += 1

                filename_raw = part.get_filename()
                if filename_raw is None:
                    filename_raw = part.get_param("name", header="content-type")
                filename = clean(decode_hdr(str(filename_raw))) if filename_raw else None
                cid = clean(str(part.get("Content-ID"))) if part.get("Content-ID") else None
                if not filename and cid:
                    # Inline images -- the signature logos the dedup exists for.
                    stem = cid.strip("<>").split("@")[0] or sha[:12]
                    ext = db.execute(
                        "SELECT sniffed_ext FROM blobs WHERE sha256=?", (sha,)
                    ).fetchone()[0]
                    filename = f"{stem}.{ext}" if ext else stem

                db.execute(
                    "INSERT OR REPLACE INTO attachments (message_id_fk, sha256, part_path,"
                    " filename, filename_raw, mime_declared, disposition, content_id,"
                    " decode_failed) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        msg_id,
                        sha,
                        part_path,
                        filename,
                        str(filename_raw) if filename_raw else None,
                        (part.get_content_type() or "").lower() or None,
                        part.get_content_disposition(),
                        cid,
                        int(decode_failed),
                    ),
                )
                stats["attachments"] += 1
            except Exception as exc:
                stats["errors"] += 1
                print(f"part failed {path} [{part_path}]: {exc}", file=sys.stderr)

        if stats["indexed"] % 500 == 0:
            db.commit()
        if args.limit and stats["indexed"] >= args.limit:
            break

    db.commit()
    db.execute("ANALYZE")
    db.commit()
    db.close()

    print("\n".join(f"{k:>12}: {v}" for k, v in stats.items()))
    print(f"\nindex: {out / 'index.db'}\nblobs: {blob_root}")
    return 0
