#!/usr/bin/env python3
"""Local browser for the attachment index built by extract.py.

Server-side paged and filtered -- 50k rows in one DOM falls over, so the
grid asks sqlite for a page at a time. Binds to localhost only; the blobs
are private mail and this has no authentication.

    ./serve.py ~/Archive/mail-attachments
    open http://127.0.0.1:8765/
"""

from __future__ import annotations

import email
import email.policy
import json
import os
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .errors import AtticError
from .text import decode_bytes, extract_body, html_to_text

try:
    from PIL import Image

    HAVE_PIL = True
except ImportError:  # optional: without it the grid serves originals
    HAVE_PIL = False

PAGE_SIZE = 120

# Cards are 178x128 CSS pixels, so 360px covers a 2x display. Serving the
# originals instead means ~10 MB per page of grid, which is what stalled it.
THUMB_PX = 360

# A readable render for the detail panel. TIFF is the main population here --
# Chrome cannot decode it at all, so /blob/ shows nothing and only a converted
# copy is viewable.
PREVIEW_PX = 1600

# Types Chrome renders natively, which therefore need no conversion.
BROWSER_IMAGE = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/bmp",
    "image/vnd.microsoft.icon",
    "image/avif",
}

# Shown as text in the detail panel rather than downloaded.
TEXTY = {
    "text/plain",
    "text/html",
    "application/xml",
    "text/csv",
    "application/json",
    "text/calendar",
}
MAX_TEXT = 256 * 1024

# Folders that swamp the gallery: DMARC reports are thousands of zipped XML
# blobs, and spam is not what anyone is looking for. Indexed, not shown by
# default -- the folder filter still reaches them.
NOISE = (".reports.dmarc", ".Junk", ".Spam", ".Trash", ".Deleted Messages")


def build_query(params: dict[str, str], noise: tuple[str, ...] = NOISE) -> tuple[str, list]:
    """Translate the filter form into a WHERE clause."""
    where, args = [], []

    if params.get("q"):
        where.append("(filename LIKE ? OR subject LIKE ? OR from_raw LIKE ? OR to_raw LIKE ?)")
        args += [f"%{params['q']}%"] * 4
    if params.get("direction") in ("in", "out", "unknown"):
        where.append("direction = ?")
        args.append(params["direction"])
    if params.get("year"):
        where.append("year = ?")
        args.append(params["year"])
    if params.get("folder"):
        where.append("folder = ?")
        args.append(params["folder"])
    if params.get("kind"):
        kind = params["kind"]
        if kind == "other":
            where.append(
                "COALESCE(sniffed_type, mime_declared, '') NOT LIKE 'image/%'"
                " AND COALESCE(sniffed_type, mime_declared, '') NOT LIKE 'application/pdf%'"
                " AND COALESCE(sniffed_type, mime_declared, '') NOT LIKE 'application/zip%'"
            )
        else:
            where.append("COALESCE(sniffed_type, mime_declared, '') LIKE ?")
            args.append(kind + "%")
    if params.get("minsize"):
        try:
            where.append("size >= ?")
            args.append(int(params["minsize"]) * 1024)
        except ValueError:
            where.pop()
    if not params.get("folder") and params.get("noise") != "1":
        where += ["folder <> ?"] * len(noise)
        args += list(noise)
    if params.get("sha"):
        sha = params["sha"]
        if len(sha) == 64 and all(c in "0123456789abcdef" for c in sha):
            where.append("sha256 = ?")
            args.append(sha)
    if params.get("dupes") == "1":
        where.append(
            "sha256 IN (SELECT sha256 FROM attachments GROUP BY sha256 HAVING count(*) > 1)"
        )

    return (" WHERE " + " AND ".join(where)) if where else "", args


SORTS = {
    "date": "date_parsed DESC, id DESC",
    "oldest": "date_parsed ASC, id ASC",
    "size": "size DESC",
    "name": "filename COLLATE NOCASE ASC",
    "dupes": "copies DESC, sha256",
}


class Handler(BaseHTTPRequestHandler):
    root: Path
    dbpath: Path
    label: str | None = None
    noise: tuple[str, ...] = NOISE

    # HTTP/1.0 (the default) closes the connection after every response, so a
    # grid of 120 cards needs 120 TCP connections and overruns the listen
    # backlog -- requests stall forever rather than fail, which is why nothing
    # showed in the console. Keep-alive needs an accurate Content-Length on
    # every response, which send_bytes and send_error both provide.
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):  # quieter console
        pass

    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.dbpath}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    # The page needs inline script and style; a blob must get neither. An XML
    # attachment carrying an XSLT processing instruction is the path this closes.
    # connect-src is not optional: it falls back to default-src, so omitting it
    # under default-src 'none' blocks the page's own fetch() calls and the grid
    # renders empty with only the static header showing.
    # object-src and frame-src both fall back to default-src, so under
    # default-src 'none' the <embed> that previews a PDF is blocked -- silently,
    # with no console message, which is what made this hard to see.
    CSP_PAGE = (
        "default-src 'none'; connect-src 'self'; img-src 'self' data:; "
        "object-src 'self'; frame-src 'self'; "
        "style-src 'unsafe-inline'; script-src 'unsafe-inline'"
    )
    CSP_BLOB = "default-src 'none'; sandbox"

    def send_bytes(
        self,
        body: bytes,
        ctype: str,
        extra: dict | None = None,
        csp: str | None = None,
        immutable: bool = False,
    ):
        # csp="" omits the header entirely; None means use the page default.
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if csp != "":
            self.send_header("Content-Security-Policy", csp or self.CSP_PAGE)
        self.send_header("X-Content-Type-Options", "nosniff")
        # Blobs and thumbs are addressed by the hash of their content, so they
        # can never change under a URL. The page and the API must never be
        # cached, or a restarted server keeps serving the old JavaScript.
        self.send_header(
            "Cache-Control", "public, max-age=31536000, immutable" if immutable else "no-store"
        )
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            if parsed.path == "/":
                self.send_bytes(PAGE.encode(), "text/html; charset=utf-8")
            elif parsed.path == "/api/meta":
                self.send_bytes(json.dumps({"label": self.label}).encode(), "application/json")
            elif parsed.path == "/api/facets":
                self.send_bytes(json.dumps(self.facets()).encode(), "application/json")
            elif parsed.path == "/api/list":
                self.send_bytes(json.dumps(self.listing(params)).encode(), "application/json")
            elif parsed.path == "/api/message":
                self.send_bytes(json.dumps(self.message(params)).encode(), "application/json")
            elif parsed.path == "/api/text":
                self.send_bytes(json.dumps(self.text(params)).encode(), "application/json")
            elif parsed.path.startswith("/thumb/"):
                self.thumb(parsed.path[7:])
            elif parsed.path.startswith("/preview/"):
                self.preview(parsed.path[9:])
            elif parsed.path.startswith("/blob/"):
                self.blob(parsed.path[6:], params)
            else:
                self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self.send_error(500, str(exc))

    def facets(self) -> dict:
        with self.db() as conn:
            return {
                "years": [
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT year FROM v_attachments WHERE year IS NOT NULL"
                        " ORDER BY year DESC"
                    )
                ],
                "folders": [
                    {"folder": r[0], "n": r[1]}
                    for r in conn.execute(
                        "SELECT folder, count(*) FROM v_attachments GROUP BY 1 ORDER BY 2 DESC"
                    )
                ],
                "totals": dict(
                    conn.execute(
                        "SELECT (SELECT count(*) FROM attachments) AS occurrences,"
                        " (SELECT count(*) FROM blobs) AS distinct_blobs,"
                        " (SELECT COALESCE(sum(size),0) FROM blobs) AS unique_bytes,"
                        " (SELECT COALESCE(sum(b.size),0) FROM attachments a"
                        "   JOIN blobs b ON b.sha256=a.sha256) AS total_bytes,"
                        " (SELECT count(*) FROM messages) AS messages"
                    ).fetchone()
                ),
            }

    def listing(self, params: dict) -> dict:
        where, args = build_query(params, self.noise)
        order = SORTS.get(params.get("sort", "date"), SORTS["date"])
        try:
            page = max(0, int(params.get("page", 0)))
        except ValueError:
            page = 0

        # Collapsed (the default) shows one card per distinct file rather than
        # one per occurrence; the copy badge says how many messages carried it.
        collapse = params.get("collapse", "1") == "1"
        cols = (
            "id, sha256, filename, mime_declared, sniffed_type, sniffed_ext,"
            " size, folder, direction, subject, from_raw, to_raw,"
            " decode_failed,"
            " (SELECT count(*) FROM attachments a2"
            "   WHERE a2.sha256 = v_attachments.sha256) AS copies"
        )

        with self.db() as conn:
            if collapse:
                total = conn.execute(
                    f"SELECT count(DISTINCT sha256) FROM v_attachments{where}", args
                ).fetchone()[0]
                bytes_matched = conn.execute(
                    f"SELECT COALESCE(sum(size),0) FROM"
                    f" (SELECT size FROM v_attachments{where} GROUP BY sha256)",
                    args,
                ).fetchone()[0]
                # The bare columns of a GROUP BY come from the row holding the
                # min()/max(), so this picks the earliest occurrence as the
                # representative rather than an arbitrary one.
                rows = conn.execute(
                    f"SELECT {cols}, MIN(date_parsed) AS date_parsed"
                    f" FROM v_attachments{where} GROUP BY sha256 ORDER BY {order}"
                    f" LIMIT {PAGE_SIZE} OFFSET {page * PAGE_SIZE}",
                    args,
                ).fetchall()
            else:
                total = conn.execute(
                    f"SELECT count(*) FROM v_attachments{where}", args
                ).fetchone()[0]
                bytes_matched = conn.execute(
                    f"SELECT COALESCE(sum(size),0) FROM v_attachments{where}", args
                ).fetchone()[0]
                rows = conn.execute(
                    f"SELECT {cols}, date_parsed FROM v_attachments{where}"
                    f" ORDER BY {order} LIMIT {PAGE_SIZE} OFFSET {page * PAGE_SIZE}",
                    args,
                ).fetchall()

        return {
            "total": total,
            "page": page,
            "pages": (total + PAGE_SIZE - 1) // PAGE_SIZE,
            "bytes": bytes_matched,
            "collapsed": collapse,
            "rows": [dict(r) for r in rows],
        }

    def message(self, params: dict) -> dict:
        """The message an attachment came from: headers, body text, part list.

        The body is returned as plain text and escaped by the client. Mail is
        full of tracking pixels and worse, so nothing from it is ever rendered
        as markup.
        """
        try:
            att_id = int(params.get("id", ""))
        except ValueError:
            return {"error": "bad id"}

        with self.db() as conn:
            row = conn.execute(
                "SELECT m.maildir_path, m.folder, m.subject, m.from_raw, m.to_raw,"
                " m.cc_raw, m.date_parsed, m.date_raw, m.message_id"
                " FROM attachments a JOIN messages m ON m.id = a.message_id_fk"
                " WHERE a.id = ?",
                (att_id,),
            ).fetchone()
            if not row:
                return {"error": "no such attachment"}
            parts = [
                dict(r)
                for r in conn.execute(
                    "SELECT a.id, a.filename, a.mime_declared, a.part_path, b.size,"
                    " b.sniffed_type, a.sha256 FROM attachments a"
                    " JOIN blobs b ON b.sha256 = a.sha256"
                    " WHERE a.message_id_fk = (SELECT message_id_fk FROM attachments"
                    "   WHERE id = ?) ORDER BY a.part_path",
                    (att_id,),
                )
            ]

        head = {
            k: row[k]
            for k in (
                "folder",
                "subject",
                "from_raw",
                "to_raw",
                "cc_raw",
                "date_parsed",
                "date_raw",
                "message_id",
                "maildir_path",
            )
        }
        body, truncated = "", False
        try:
            raw = Path(row["maildir_path"]).read_bytes()
            try:
                msg = email.message_from_bytes(raw, policy=email.policy.default)
            except Exception:
                msg = email.message_from_bytes(raw)
            body = extract_body(msg)
            if len(body) > 200_000:
                body, truncated = body[:200_000], True
        except FileNotFoundError:
            body = "(the message file is no longer at the path recorded here)"
        except Exception as exc:
            body = f"(could not read the message: {exc})"

        return {"head": head, "body": body, "truncated": truncated, "parts": parts}

    def text(self, params: dict) -> dict:
        """A text blob decoded server-side.

        utf-8 then cp1252 then latin-1: this is German mail from 2002 onward,
        so cp1252 is the common hit and a client-side utf-8 decode would turn
        every umlaut into a replacement character.
        """
        sha = self.valid(params.get("sha", ""))
        if not sha:
            return {"error": "bad digest"}
        path = self.root / "blobs" / sha[:2] / sha
        if not path.exists():
            return {"error": "blob missing from disk"}
        with self.db() as conn:
            row = conn.execute(
                "SELECT sniffed_type FROM blobs WHERE sha256 = ?", (sha,)
            ).fetchone()
        raw = path.read_bytes()
        truncated = len(raw) > MAX_TEXT
        raw = raw[:MAX_TEXT]
        text, enc = decode_bytes(raw)
        if row and row["sniffed_type"] == "text/html":
            text = html_to_text(text)  # the same stripper the message pane uses
        return {"text": text, "charset": enc, "truncated": truncated}

    @staticmethod
    def valid(sha: str) -> str | None:
        sha = sha.split("?")[0].split("/")[0]
        if len(sha) == 64 and all(c in "0123456789abcdef" for c in sha):
            return sha
        return None

    def thumb(self, sha: str):
        """A cached downscale for the grid, falling back to the original."""
        return self.render(sha, "thumbs", THUMB_PX)

    def preview(self, sha: str):
        """A cached, browser-renderable copy for the detail panel."""
        return self.render(sha, "previews", PREVIEW_PX)

    def render(self, sha: str, subdir: str, max_px: int):
        sha = self.valid(sha)
        if not sha:
            return self.send_error(400, "bad digest")
        if not HAVE_PIL:
            return self.blob(sha, {})

        cache = self.root / subdir / sha[:2]
        for ext, ctype in (("jpg", "image/jpeg"), ("png", "image/png")):
            hit = cache / f"{sha}.{ext}"
            if hit.exists():
                return self.send_bytes(
                    hit.read_bytes(), ctype, csp=self.CSP_BLOB, immutable=True
                )

        src = self.root / "blobs" / sha[:2] / sha
        if not src.exists():
            return self.send_error(404, "blob missing from disk")
        try:
            with Image.open(src) as im:
                im.load()
                alpha = im.mode in ("RGBA", "LA") or (
                    im.mode == "P" and "transparency" in im.info
                )
                im = im.convert("RGBA" if alpha else "RGB")
                # thumbnail() only ever shrinks, so a 1x1 tracking pixel stays
                # 1x1 and the CSS scales it up. A multi-page TIFF gives frame 0,
                # which is the right answer for a quick look.
                im.thumbnail((max_px, max_px), Image.LANCZOS)
                cache.mkdir(parents=True, exist_ok=True)
                ext = "png" if alpha else "jpg"
                tmp = cache / f"{sha}.{os.getpid()}.part"
                if alpha:
                    im.save(tmp, "PNG", optimize=True)
                else:
                    im.save(tmp, "JPEG", quality=80, optimize=True)
                tmp.replace(cache / f"{sha}.{ext}")
            data = (cache / f"{sha}.{ext}").read_bytes()
            return self.send_bytes(
                data, "image/png" if alpha else "image/jpeg", csp=self.CSP_BLOB, immutable=True
            )
        except Exception:
            # Anything Pillow cannot open -- a CMYK oddity, a truncated file --
            # still deserves a shot at rendering from the original.
            return self.blob(sha, {})

    def blob(self, sha: str, params: dict):
        sha = self.valid(sha)
        if not sha:
            return self.send_error(400, "bad digest")
        with self.db() as conn:
            row = conn.execute(
                "SELECT b.size, b.sniffed_type, b.sniffed_ext,"
                " (SELECT filename FROM attachments WHERE sha256=b.sha256"
                "   AND filename IS NOT NULL LIMIT 1) AS filename"
                " FROM blobs b WHERE b.sha256 = ?",
                (sha,),
            ).fetchone()
        if not row:
            return self.send_error(404)

        path = self.root / "blobs" / sha[:2] / sha
        if not path.exists():
            return self.send_error(404, "blob missing from disk")
        data = path.read_bytes()

        ctype = row["sniffed_type"] or "application/octet-stream"
        # Only hand the browser types it renders safely inline.
        inline_ok = (
            ctype.startswith("image/")
            or ctype == "application/pdf"
            or ctype in ("text/plain", "application/xml")
        )
        if params.get("dl") == "1" or not inline_ok:
            ctype = "application/octet-stream"
        name = row["filename"] or f"{sha[:12]}.{row['sniffed_ext'] or 'bin'}"
        name = (
            "".join(c for c in name if c.isprintable() and c not in '"\\/\r\n')[:120]
            or sha[:12]
        )
        disp = "inline" if inline_ok and params.get("dl") != "1" else "attachment"
        self.send_bytes(
            data,
            ctype,
            {"Content-Disposition": f'{disp}; filename="{name}"'},
            csp=self.CSP_BLOB,
            immutable=True,
        )


PAGE = r"""<!DOCTYPE html>
<html lang="en-NZ"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mail Attachments</title>
<style>
:root{--bg:#fbfaf8;--panel:#fff;--ink:#1c1b19;--muted:#6b6862;--line:#e3e0da;
 --accent:#7a5c3e;--accent-soft:#f0e9e1;--in:#3a6b52;--out:#5a4a8a;--radius:9px}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
 --bg:#17161a;--panel:#201f24;--ink:#eceae6;--muted:#9b968e;--line:#33313a;
 --accent:#c9a87c;--accent-soft:#2c2830;--in:#7fc0a0;--out:#a99ae0}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
header{position:sticky;top:0;z-index:10;background:var(--panel);border-bottom:1px solid var(--line);padding:10px 16px}
h1{margin:0 0 8px;font-size:15px;letter-spacing:.02em;font-weight:600}
h1 small{font-weight:400;color:var(--muted);margin-left:8px;font-size:12px}
.filters{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
input,select,button{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);
 border-radius:var(--radius);padding:5px 9px}
input[type=search]{min-width:220px;flex:1 1 220px}
button{cursor:pointer;background:var(--accent-soft);border-color:transparent}
button:hover{border-color:var(--accent)}
button.on{background:var(--accent);color:var(--bg);font-weight:600}
main{padding:14px 16px 60px}
.bar{display:flex;justify-content:space-between;align-items:center;gap:12px;color:var(--muted);
 font-size:12px;flex-wrap:wrap;margin-top:8px;padding-top:8px;border-top:1px solid var(--line)}
.pagenav{display:flex;gap:6px;align-items:center;margin-right:auto}
.pagenav button{padding:2px 9px;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(178px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
 overflow:hidden;display:flex;flex-direction:column}
.thumb{height:128px;display:grid;place-items:center;background:var(--accent-soft);overflow:hidden;position:relative}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.ext{font:600 20px/1 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--accent);letter-spacing:.06em}
.meta{padding:8px 9px;display:flex;flex-direction:column;gap:3px;min-width:0}
.fn{font-weight:600;font-size:12px;word-break:break-all;line-height:1.35;
 display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sub{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tags{display:flex;gap:4px;flex-wrap:wrap;margin-top:2px}
.tag{font-size:10px;padding:1px 6px;border-radius:99px;background:var(--accent-soft);color:var(--muted)}
.tag.in{color:var(--in)}.tag.out{color:var(--out)}
.tag.dup{background:var(--accent);color:var(--bg);font-weight:600}
a{color:inherit;text-decoration:none}
.pager{display:flex;gap:8px;justify-content:center;align-items:center;margin-top:22px}
dialog{border:1px solid var(--line);border-radius:var(--radius);background:var(--panel);color:var(--ink);
 max-width:min(900px,94vw);width:100%;padding:0}
dialog::backdrop{background:#0009}
.dhead{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
.dbody{padding:14px 16px;max-height:70vh;overflow:auto}
.dbody img{max-width:100%;border-radius:6px;background:var(--accent-soft)}
table{width:100%;border-collapse:collapse;font-size:12px}
td{padding:4px 8px 4px 0;vertical-align:top;border-bottom:1px solid var(--line)}
td:first-child{color:var(--muted);white-space:nowrap;width:1%}
.empty{text-align:center;color:var(--muted);padding:60px 20px}
@media(max-width:600px){.grid{grid-template-columns:repeat(auto-fill,minmax(140px,1fr))}
 input[type=search]{min-width:100%}}
</style></head><body>
<header>
 <h1><span id="label">Mail Attachments</span> <small id="totals"></small></h1>
 <div class="filters">
  <input type="search" id="q" placeholder="filename, subject, sender…">
  <select id="direction"><option value="">in + out</option><option value="in">incoming</option>
   <option value="out">outgoing</option><option value="unknown">unknown</option></select>
  <select id="kind"><option value="">any type</option><option value="image/">images</option>
   <option value="application/pdf">PDFs</option><option value="application/zip">zips</option>
   <option value="other">other</option></select>
  <select id="year"><option value="">any year</option></select>
  <select id="folder"><option value="">any folder</option></select>
  <select id="sort"><option value="date">newest</option><option value="oldest">oldest</option>
   <option value="size">largest</option><option value="name">by name</option>
   <option value="dupes">most copies</option></select>
  <input type="number" id="minsize" placeholder="min KB" style="width:92px" min="0">
  <button id="dupes" title="Only files that appear more than once">duplicates</button>
  <button id="everycopy" title="One card per message instead of one per file">every copy</button>
  <button id="noise" title="Include DMARC reports, junk, spam and trash">noise</button>
  <button id="reset">reset</button>
 </div>
 <div class="bar"><span id="count"></span>
  <span class="pagenav" id="navtop"></span><span id="bytes"></span></div>
</header>
<main>
 <div class="grid" id="grid"></div>
 <div class="pager" id="pager"></div>
</main>
<dialog id="detail"><div class="dhead"><strong id="dtitle"></strong>
 <button onclick="detail.close()">close</button></div>
 <div class="dbody" id="dbody"></div></dialog>
<script>
const $ = s => document.querySelector(s);
const state = {page:0, dupes:0, noise:0, sha:"", everycopy:0};
const FIELDS = ["q","direction","kind","year","folder","sort","minsize"];

const kb = n => n < 1024 ? n+" B"
  : n < 1048576 ? (n/1024).toFixed(0)+" KB"
  : n < 1073741824 ? (n/1048576).toFixed(1)+" MB" : (n/1073741824).toFixed(2)+" GB";
const esc = s => (s??"").replace(/[<>&"]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));

function params(){
  const p = new URLSearchParams();
  FIELDS.forEach(f => { const v = $("#"+f).value.trim(); if(v) p.set(f, v); });
  if(state.sha) p.set("sha", state.sha);
  p.set("collapse", state.everycopy ? "0" : "1");
  if(state.dupes) p.set("dupes","1");
  if(state.noise) p.set("noise","1");
  p.set("page", state.page);
  return p;
}

async function load(){
  const r = await fetch("/api/list?" + params());
  const d = await r.json();
  $("#count").innerHTML = d.total.toLocaleString("en-NZ")
    + (d.collapsed ? " file" : " attachment") + (d.total===1?"":"s")
    + (d.pages > 1 ? ` · page ${d.page+1} of ${d.pages}` : "")
    + (state.sha ? ` · <span class="tag dup">one file, ${state.sha.slice(0,12)}…</span>`
        + ` <button style="padding:1px 8px;font-size:11px" onclick="state.sha='';load()">clear</button>` : "");
  $("#bytes").textContent = kb(d.bytes);
  $("#grid").innerHTML = d.rows.length ? d.rows.map(card).join("")
    : '<div class="empty" style="grid-column:1/-1">Nothing matches these filters.</div>';
  const nav = d.pages > 1 ? `
    <button ${d.page===0?"disabled":""} onclick="go(${d.page-1})">← previous</button>
    <span style="color:var(--muted);font-size:12px">${d.page+1} / ${d.pages}</span>
    <button ${d.page+1>=d.pages?"disabled":""} onclick="go(${d.page+1})">next →</button>` : "";
  // Both pagers, because the bottom one scrolls out of view the moment you
  // use it and the page then looks unchanged.
  $("#pager").innerHTML = nav;
  $("#navtop").innerHTML = nav;
  window.rows = Object.fromEntries(d.rows.map(r => [r.id, r]));
}
function go(p){ state.page = p; load(); scrollTo({top:0,behavior:"smooth"}); }

function card(r){
  const type = r.sniffed_type || r.mime_declared || "";
  const thumb = type.startsWith("image/")
    ? `<img loading="lazy" src="/thumb/${r.sha256}" alt="">`
    : `<span class="ext">${esc((r.sniffed_ext || (r.filename||"").split(".").pop() || "bin").slice(0,5).toUpperCase())}</span>`;
  const date = r.date_parsed ? r.date_parsed.slice(0,10) : "no date";
  return `<article class="card" onclick="show(${r.id})" style="cursor:pointer">
    <div class="thumb">${thumb}</div>
    <div class="meta">
      <div class="fn">${esc(r.filename || "(unnamed)")}</div>
      <div class="sub">${date} · ${kb(r.size)}</div>
      <div class="tags">
        <span class="tag ${r.direction}">${r.direction}</span>
        ${r.copies>1 ? `<span class="tag dup">${r.copies}\u00d7</span>`:""}
        ${r.decode_failed ? '<span class="tag">raw</span>':""}
      </div>
    </div></article>`;
}

// Types Chrome decodes itself. Anything else image-shaped -- TIFF above all,
// which Chrome cannot read at all -- goes through /preview/ for a converted copy.
const BROWSER_IMAGE = new Set(["image/png","image/jpeg","image/gif","image/webp",
  "image/bmp","image/vnd.microsoft.icon","image/avif"]);
const TEXTY = new Set(["text/plain","text/html","application/xml","text/csv",
  "application/json","text/calendar"]);

function previewFor(r){
  const t = r.sniffed_type || r.mime_declared || "";
  if(t === "application/pdf")
    return `<embed src="/blob/${r.sha256}" type="application/pdf"
              style="width:100%;height:56vh;border-radius:6px">`;
  if(BROWSER_IMAGE.has(t))  return `<img src="/blob/${r.sha256}" alt="">`;
  if(t.startsWith("image/")) return `<img src="/preview/${r.sha256}" alt="">`;
  if(TEXTY.has(t)) return `<pre id="textpane" style="white-space:pre-wrap;
      word-break:break-word;background:var(--bg);padding:12px;border-radius:6px;
      font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
      max-height:46vh;overflow:auto">reading…</pre>`;
  return "";
}

function show(id){
  const r = window.rows[id];
  const type = r.sniffed_type || r.mime_declared || "";
  $("#dtitle").textContent = r.filename || "(unnamed attachment)";
  const preview = previewFor(r);
  const row = (k,v) => v ? `<tr><td>${k}</td><td>${esc(String(v))}</td></tr>` : "";
  $("#dbody").innerHTML = preview + `<table>
    ${row("subject", r.subject)}${row("from", r.from_raw)}${row("to", r.to_raw)}
    ${row("date", r.date_parsed)}${row("folder", r.folder)}${row("direction", r.direction)}
    ${row("declared", r.mime_declared)}${row("sniffed", r.sniffed_type)}
    ${row("size", kb(r.size))}${row("copies in archive", r.copies)}
    ${row("sha256", r.sha256)}
   </table>
   <p style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap">
     <a href="/blob/${r.sha256}?dl=1" download><button>download</button></a>
     <button onclick="showMessage(${r.id})">open the message</button>
     ${r.copies>1 ? `<button onclick="detail.close();seeCopies('${r.sha256}')">see all ${r.copies} copies</button>`:""}
   </p>
   <div id="msgpane"></div>`;
  detail.showModal();
  // Text is fetched rather than inlined so the server can pick the encoding;
  // twenty-year-old German mail is mostly cp1252, not utf-8.
  if(TEXTY.has(type)) fetch("/api/text?sha=" + r.sha256).then(x => x.json()).then(d => {
    const pane = $("#textpane");
    if(!pane) return;
    pane.textContent = d.error ? d.error
      : (d.text || "(empty file)") + (d.truncated ? "\n\n… truncated" : "");
  });
}

async function showMessage(id){
  const pane = $("#msgpane");
  pane.innerHTML = '<p style="color:var(--muted)">reading the message…</p>';
  const d = await fetch("/api/message?id=" + id).then(r => r.json());
  if(d.error){ pane.innerHTML = `<p style="color:var(--muted)">${esc(d.error)}</p>`; return; }
  const h = d.head;
  const row = (k,v) => v ? `<tr><td>${k}</td><td>${esc(String(v))}</td></tr>` : "";
  // The body is inserted as text, never as markup -- mail carries tracking
  // pixels and worse, and none of it should render or phone home.
  pane.innerHTML = `<hr style="border:0;border-top:1px solid var(--line);margin:16px 0">
    <table>${row("subject",h.subject)}${row("from",h.from_raw)}${row("to",h.to_raw)}
      ${row("cc",h.cc_raw)}${row("date",h.date_parsed||h.date_raw)}${row("folder",h.folder)}
      ${row("file",h.maildir_path)}</table>
    <p style="margin:12px 0 4px;color:var(--muted);font-size:12px">
      ${d.parts.length} attachment${d.parts.length===1?"":"s"} on this message:
      ${d.parts.map(x=>`<a href="/blob/${x.sha256}?dl=1" download
        style="text-decoration:underline">${esc(x.filename||x.sha256.slice(0,8))}</a>`).join(", ")}
    </p>
    <pre style="white-space:pre-wrap;word-break:break-word;background:var(--bg);
      padding:12px;border-radius:6px;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
      max-height:40vh;overflow:auto"></pre>
    ${d.truncated?'<p style="color:var(--muted);font-size:11px">(body truncated)</p>':""}`;
  pane.querySelector("pre").textContent = d.body;
}
function seeCopies(sha){
  // Pin the grid to one digest: every message that carried these exact bytes.
  state.sha = sha; state.page = 0; state.noise = 1;
  $("#noise").classList.add("on");
  FIELDS.forEach(f => $("#"+f).value = f === "sort" ? "oldest" : "");
  state.dupes = 0; $("#dupes").classList.remove("on");
  load();
}

let timer;
FIELDS.forEach(f => $("#"+f).addEventListener("input", () => {
  clearTimeout(timer); timer = setTimeout(() => { state.page = 0; state.sha = ""; load(); }, 220);
}));
$("#dupes").onclick = e => { state.dupes ^= 1; e.target.classList.toggle("on", !!state.dupes); state.page=0; load(); };
$("#everycopy").onclick = e => { state.everycopy ^= 1;
  e.target.classList.toggle("on", !!state.everycopy); state.page=0; load(); };
$("#noise").onclick = e => { state.noise ^= 1; e.target.classList.toggle("on", !!state.noise); state.page=0; load(); };
$("#reset").onclick = () => { FIELDS.forEach(f => $("#"+f).value = f==="sort"?"date":"");
  state.dupes = state.noise = state.page = state.everycopy = 0; state.sha = "";
  $("#dupes").classList.remove("on"); $("#noise").classList.remove("on");
  $("#everycopy").classList.remove("on"); load(); };

fetch("/api/meta").then(r=>r.json()).then(m => {
  if(m.label){
    $("#label").textContent = m.label;
    document.title = m.label + " — attachments";
    // Two mailboxes in two tabs look identical otherwise, which is how you
    // end up reading the wrong person's mail. Derive a hue from the label.
    let h = 0; for(const c of m.label) h = (h*31 + c.charCodeAt(0)) % 360;
    document.documentElement.style.setProperty("--accent", `hsl(${h} 42% 62%)`);
    document.documentElement.style.setProperty("--accent-soft", `hsl(${h} 30% 16%)`);
  }
});

fetch("/api/facets").then(r=>r.json()).then(d => {
  $("#year").insertAdjacentHTML("beforeend", d.years.map(y=>`<option>${y}</option>`).join(""));
  $("#folder").insertAdjacentHTML("beforeend",
    d.folders.map(f=>`<option value="${esc(f.folder)}">${esc(f.folder)} (${f.n})</option>`).join(""));
  const t = d.totals, saved = t.total_bytes - t.unique_bytes;
  $("#totals").textContent =
    `${t.occurrences.toLocaleString("en-NZ")} occurrences · ${t.distinct_blobs.toLocaleString("en-NZ")} distinct `
    + `· ${kb(t.unique_bytes)} stored, ${kb(saved)} saved by dedup`;
});
load();
</script></body></html>
"""


def run(args) -> int:
    """Serve the browser. Arguments come from the CLI."""

    root = args.root.expanduser().resolve()
    dbpath = root / "index.db"
    if not dbpath.exists():
        raise AtticError(f"no index at {dbpath}\nrun: attic extract <maildir> --out {root}")

    Handler.root, Handler.dbpath = root, dbpath
    Handler.label = args.label
    if args.hide:
        Handler.noise = tuple(args.hide)

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 128  # the default 5 is far too few for a grid
        allow_reuse_address = True

    server = Server(("127.0.0.1", args.port), Handler)
    if not HAVE_PIL:
        print(
            "note: Pillow not installed -- serving full-size images, "
            "which makes the grid slow. pip install Pillow"
        )
    print(
        f"attachment browser{' for ' + args.label if args.label else ''}: "
        f"http://127.0.0.1:{args.port}/\n(localhost only, no auth; ^C to stop)"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0
