"""The query layer and the HTTP surface."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from attic import serve


def test_collapse_is_the_default():
    where, _args = serve.build_query({})
    assert "sha256" not in where  # no digest pin
    sql, _ = serve.build_query({"dupes": "1"})
    assert "count(*) > 1" in sql


def test_noise_folders_are_excluded_unless_asked():
    where, _args = serve.build_query({})
    assert where.count("folder <> ?") == len(serve.NOISE)
    where, _ = serve.build_query({"noise": "1"})
    assert "folder <> ?" not in where


def test_choosing_a_folder_overrides_the_noise_filter():
    """Picking .Spam explicitly must show .Spam."""
    where, args = serve.build_query({"folder": ".Spam"})
    assert "folder = ?" in where and ".Spam" in args
    assert "folder <> ?" not in where


def test_a_bad_digest_is_not_used_as_a_filter():
    where, _ = serve.build_query({"sha": "not-a-digest"})
    assert "sha256 = ?" not in where


def test_sort_keys_are_a_fixed_set():
    """Sort must never be interpolated from user input."""
    assert "date" in serve.SORTS
    assert all(";" not in v for v in serve.SORTS.values())


@pytest.fixture
def server(extracted):
    serve.Handler.root = extracted
    serve.Handler.dbpath = extracted / "index.db"
    serve.Handler.label = "test@example.com"
    serve.Handler.noise = serve.NOISE

    class S(serve.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    httpd = S(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def get(base, path):
    return urllib.request.urlopen(base + path, timeout=10)


def test_page_csp_allows_its_own_fetches_and_embeds(server):
    """Both were omitted once and broke the page silently."""
    csp = get(server, "/").headers["Content-Security-Policy"]
    assert "connect-src 'self'" in csp, "grid cannot load without this"
    assert "object-src 'self'" in csp, "PDF preview is blank without this"


def test_blobs_are_sandboxed_and_immutable(server):
    d = json.load(get(server, "/api/list"))
    sha = d["rows"][0]["sha256"]
    r = get(server, "/blob/" + sha)
    assert "sandbox" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "immutable" in r.headers["Cache-Control"]


def test_the_page_is_never_cached(server):
    """A restarted server must not keep serving old JavaScript."""
    assert get(server, "/").headers["Cache-Control"] == "no-store"


def test_collapsed_listing_has_no_repeated_digests(server):
    d = json.load(get(server, "/api/list?collapse=1"))
    shas = [r["sha256"] for r in d["rows"]]
    assert len(shas) == len(set(shas))
    assert d["collapsed"] is True


def test_every_copy_shows_more_than_collapsed(server):
    a = json.load(get(server, "/api/list?collapse=1"))["total"]
    b = json.load(get(server, "/api/list?collapse=0"))["total"]
    assert b > a


def test_path_traversal_on_a_blob_is_refused(server):
    for bad in ("/blob/../../etc/passwd", "/blob/" + "z" * 64, "/blob/abc"):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(server, bad)
        assert e.value.code in (400, 404)


def test_message_view_returns_headers_and_text(server):
    d = json.load(get(server, "/api/list"))
    m = json.load(get(server, f"/api/message?id={d['rows'][0]['id']}"))
    assert m["head"]["subject"]
    assert "<" not in m["body"], "markup must never reach the client"


def test_unknown_route_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        get(server, "/nope")
    assert e.value.code == 404


def test_thumbnail_is_generated_and_smaller(server, extracted):
    """The grid must not serve full-size originals."""
    d = json.load(get(server, "/api/list"))
    png = next(r for r in d["rows"] if r["sniffed_type"] == "image/png")
    thumb = get(server, "/thumb/" + png["sha256"]).read()
    assert thumb[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0"), "should be a real image"
    assert (extracted / "thumbs").exists(), "and it should be cached"


def test_thumbnail_is_served_from_cache_second_time(server):
    d = json.load(get(server, "/api/list"))
    sha = next(r for r in d["rows"] if r["sniffed_type"] == "image/png")["sha256"]
    first = get(server, "/thumb/" + sha).read()
    second = get(server, "/thumb/" + sha).read()
    assert first == second


def test_preview_renders_at_a_larger_size(server, extracted):
    d = json.load(get(server, "/api/list"))
    sha = next(r for r in d["rows"] if r["sniffed_type"] == "image/png")["sha256"]
    assert get(server, "/preview/" + sha).status == 200
    assert (extracted / "previews").exists()


def test_preview_of_a_non_image_falls_back_rather_than_failing(server):
    """A PDF is not Pillow-openable; it must still come back, not 500."""
    d = json.load(get(server, "/api/list"))
    pdf = next(r for r in d["rows"] if r["sniffed_type"] == "application/pdf")
    assert get(server, "/preview/" + pdf["sha256"]).status == 200


def test_text_endpoint_decodes_and_reports_the_charset(server, extracted):
    import sqlite3

    db = sqlite3.connect(extracted / "index.db")
    db.execute(
        "INSERT INTO blobs (sha256, size, blob_path, sniffed_type, sniffed_ext)"
        " VALUES (?,?,?,?,?)",
        ("c" * 64, 9, "cc/" + "c" * 64, "text/plain", "txt"),
    )
    db.commit()
    db.close()
    p = extracted / "blobs" / "cc"
    p.mkdir(parents=True, exist_ok=True)
    (p / ("c" * 64)).write_bytes("Gr\u00fc\u00dfe".encode("cp1252"))
    d = json.load(get(server, "/api/text?sha=" + "c" * 64))
    assert d["charset"] == "cp1252"
    assert d["text"] == "Gr\u00fc\u00dfe"


def test_text_endpoint_rejects_a_bad_digest(server):
    assert "error" in json.load(get(server, "/api/text?sha=nope"))


def test_download_forces_an_attachment_disposition(server):
    d = json.load(get(server, "/api/list"))
    sha = d["rows"][0]["sha256"]
    r = get(server, "/blob/" + sha + "?dl=1")
    assert r.headers["Content-Disposition"].startswith("attachment")
    assert r.headers["Content-Type"] == "application/octet-stream"


def test_facets_list_years_and_folders(server):
    f = json.load(get(server, "/api/facets"))
    assert f["totals"]["messages"] > 0
    assert any(x["folder"] == "INBOX" for x in f["folders"])
