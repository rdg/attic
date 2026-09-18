"""Character sets and markup stripping."""

from attic.text import decode_bytes, extract_body, html_to_text

from .conftest import simple


def test_utf8_wins_when_valid():
    text, enc = decode_bytes("Grüße".encode())
    assert (text, enc) == ("Grüße", "utf-8")


def test_cp1252_fallback_keeps_umlauts():
    """The case this ladder exists for: pre-UTF-8 German mail."""
    raw = "Grüße aus München".encode("cp1252")
    text, enc = decode_bytes(raw)
    assert enc == "cp1252"
    assert text == "Grüße aus München"
    assert "�" not in text


def test_utf8_only_would_have_mangled_it():
    """Stated as a test so the ladder is not quietly removed later."""
    raw = "Grüße".encode("cp1252")
    assert "�" in raw.decode("utf-8", "replace")
    assert "�" not in decode_bytes(raw)[0]


def test_cp1252_curly_quotes_beat_latin1():
    raw = "he said “hello”".encode("cp1252")
    assert decode_bytes(raw)[0] == "he said “hello”"


def test_undecodable_bytes_still_return_a_string():
    text, enc = decode_bytes(b"\x81\x8d\x8f\x90\x9d")
    assert isinstance(text, str) and enc == "latin-1"


def test_html_to_text_drops_script_and_style():
    html = "<style>p{color:red}</style><script>evil()</script><p>Kept</p>"
    out = html_to_text(html)
    assert "evil" not in out and "color:red" not in out
    assert "Kept" in out


def test_html_to_text_unescapes_and_breaks_lines():
    assert html_to_text("<p>a &amp; b</p><br>c") == "a & b\n\nc"


def test_extract_body_prefers_plain_text():
    m = simple()
    m.add_alternative("<p>rich</p>", subtype="html")
    body = extract_body(m)
    assert "Body text." in body
    assert "<p>" not in body


def test_extract_body_falls_back_to_html_as_text():
    from email.message import EmailMessage

    m = EmailMessage()
    m["Subject"] = "html only"
    m.set_content("<h1>Only HTML</h1>", subtype="html")
    assert "Only HTML" in extract_body(m)
    assert "<h1>" not in extract_body(m)


def test_attachment_only_message_says_so():
    from email.message import EmailMessage

    m = EmailMessage()
    m["Subject"] = "nothing"
    m.set_content(b"\x00\x01", maintype="application", subtype="octet-stream")
    assert "attachments only" in extract_body(m)
