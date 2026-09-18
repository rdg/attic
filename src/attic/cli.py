"""The `attic` command.

Argument parsing lives here so that extract and serve stay importable as
libraries, which is what makes them testable without a real archive.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from . import __version__
from .errors import AtticError


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="attic", description="Extract and browse the attachments in a mail archive."
    )
    ap.add_argument("--version", action="version", version=f"attic {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    ex = sub.add_parser(
        "extract",
        help="index a Maildir tree's attachments",
        description="Walk a Maildir tree, extract every attachment, index it.",
    )
    ex.add_argument("maildir", type=Path, help="root of the Maildir tree")
    ex.add_argument(
        "--out",
        required=True,
        type=Path,
        help="output directory for index.db and blobs/. Keep this "
        "outside any git repo: it holds private mail.",
    )
    ex.add_argument(
        "--me",
        action="append",
        default=[],
        metavar="ADDRESS",
        help="an address or @domain that counts as you, deciding "
        "incoming from outgoing. Repeatable.",
    )
    ex.add_argument(
        "--min-size",
        type=int,
        default=1,
        metavar="BYTES",
        help="skip parts smaller than this (default 1)",
    )
    ex.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="stop after N new messages, for trying it out",
    )
    ex.add_argument(
        "--reparse",
        action="store_true",
        help="re-read messages already indexed instead of skipping",
    )

    sv = sub.add_parser(
        "serve",
        help="browse an index in the browser",
        description="Serve a local, filterable browser over an extracted index.",
    )
    sv.add_argument("root", type=Path, help="the --out directory extract wrote")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument(
        "--label",
        default=None,
        metavar="NAME",
        help="whose mailbox this is. Shown in the header and tab "
        "title, and picks the accent colour, so two mailboxes "
        "open at once cannot be confused.",
    )
    sv.add_argument(
        "--hide",
        action="append",
        default=None,
        metavar="FOLDER",
        help="folder hidden until the 'noise' button is pressed. "
        "Repeatable; replaces the defaults.",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "extract":
            from . import extract

            return extract.run(args)
        from . import serve

        return serve.run(args)
    except AtticError as exc:
        print(f"attic: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
