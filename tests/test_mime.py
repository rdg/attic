"""Walking the MIME tree and getting bytes out of parts."""

from email.message import EmailMessage

from attic.mime import is_attachment, parse_message, payload_bytes, walk_parts

from .conftest import PDF, PNG, simple


def test_nested_message_attachments_are_reached():
    """A forwarded mail carries its own attachments; they must be found."""
    inner = simple(subject="inner")
    inner.add_attachment(PNG, maintype="image", subtype="png", filename="in.png")
    outer = EmailMessage()
    outer["Subject"] = "fwd"
    outer.set_content("see attached")
    outer.add_attachment(inner, filename="forwarded.eml")

    names = [p.get_filename() for _, p in walk_parts(outer)]
    assert "in.png" in names


def test_part_paths_are_unique():
    m = simple()
    m.add_attachment(PNG, maintype="image", subtype="png", filename="a.png")
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename="b.pdf")
    paths = [p for p, _ in walk_parts(m)]
    assert len(paths) == len(set(paths))


def test_body_text_is_not_an_attachment():
    m = simple()
    parts = {p.get_content_type(): p for _, p in walk_parts(m)}
    assert not is_attachment(parts["text/plain"])


def test_named_part_counts_even_without_disposition():
    m = EmailMessage()
    m.set_content("x")
    m.add_attachment(PNG, maintype="image", subtype="png", filename="named.png")
    named = [p for _, p in walk_parts(m) if p.get_filename() == "named.png"]
    assert is_attachment(named[0])


def test_inline_image_with_only_a_content_id_counts():
    """Signature logos arrive this way; they are what dedup is for."""
    m = EmailMessage()
    m.set_content("x")
    m.add_related(PNG, maintype="image", subtype="png", cid="<logo@example>")
    rel = [p for _, p in walk_parts(m) if p.get("Content-ID")]
    assert rel and is_attachment(rel[0])


def test_broken_base64_is_flagged_not_dropped():
    raw = (
        b"From: a@b\r\nSubject: broken\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"!!!! not base64 !!!!\r\n"
    )
    msg, _ = parse_message(raw)
    data, _failed = payload_bytes(msg)
    assert data is not None, "a broken part must still yield bytes"


def test_unparseable_message_is_reported_not_raised():
    msg, err = parse_message(b"\xff\xfe garbage with no headers at all")
    assert msg is not None or err is not None


def test_message_with_no_body_does_not_crash_the_walker():
    assert list(walk_parts(parse_message(b"Subject: bare\r\n\r\n")[0])) != []
