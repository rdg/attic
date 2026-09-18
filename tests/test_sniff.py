"""The sniffer decides what everything downstream can do with a file."""

from attic.sniff import sniff

from .conftest import PDF, PIXEL, PNG


def test_identifies_common_formats():
    assert sniff(PNG) == ("image/png", "png")
    assert sniff(PIXEL) == ("image/gif", "gif")
    assert sniff(PDF) == ("application/pdf", "pdf")
    assert sniff(b"\xff\xd8\xff\xe0" + b"\x00" * 64) == ("image/jpeg", "jpg")
    assert sniff(b"II*\x00" + b"\x00" * 64) == ("image/tiff", "tif")


def test_zip_containers_are_told_apart():
    """A .docx and a .zip differ only by a marker inside the archive."""
    assert sniff(b"PK\x03\x04" + b"x" * 40 + b"word/document.xml")[1] == "docx"
    assert sniff(b"PK\x03\x04" + b"x" * 40 + b"xl/workbook.xml")[1] == "xlsx"
    assert sniff(b"PK\x03\x04" + b"x" * 40 + b"ppt/slides")[1] == "pptx"
    assert sniff(b"PK\x03\x04" + b"nothing recognisable") == ("application/zip", "zip")


def test_riff_subtypes_need_the_inner_tag():
    assert sniff(b"RIFF\x00\x00\x00\x00WAVEfmt ") == ("audio/wav", "wav")
    assert sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ("image/webp", "webp")


def test_text_and_xml():
    assert sniff(b"plain words") == ("text/plain", "txt")
    assert sniff(b'<?xml version="1.0"?><r/>') == ("application/xml", "xml")
    assert sniff(b"<!DOCTYPE html><html>") == ("text/html", "html")


def test_unrecognised_binary_is_unknown_not_guessed():
    """Returning None matters: it is what stops the browser being lied to."""
    assert sniff(b"\x00\x01\x02\xfe\xff\x7f\x80" * 20) == (None, None)


def test_empty_input_does_not_raise():
    assert sniff(b"") == ("text/plain", "txt")


def test_declared_type_is_irrelevant():
    """An Illustrator file declares PostScript and is really a PDF."""
    assert sniff(PDF)[0] == "application/pdf"
