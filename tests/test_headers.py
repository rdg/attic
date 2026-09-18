"""Headers, dates and the incoming/outgoing call."""

from pathlib import Path

from attic.headers import SENT_FOLDER, clean, decode_hdr, folder_of, when

from .conftest import simple


def test_rfc2047_is_decoded():
    assert decode_hdr("=?utf-8?q?Gr=C3=BC=C3=9Fe?=") == "Grüße"


def test_malformed_encoded_word_returns_something_usable():
    """Twenty-year-old mail breaks RFC 2047 constantly; never raise."""
    assert decode_hdr("=?bogus-charset?q?abc?=") is not None
    assert decode_hdr(None) is None


def test_clean_strips_nuls_and_collapses_whitespace():
    assert clean("a\x00b   c\n\nd") == "ab c d"
    assert clean("   ") is None


def test_sent_folder_matching_is_not_english_only():
    for name in ("Sent", "Gesendet", "sent-mail", "Sent Messages", "Postausgang", "Outbox"):
        assert SENT_FOLDER.search(name), name


def test_sent_folder_does_not_match_substrings():
    """A folder called 'consent' is not sent mail."""
    assert not SENT_FOLDER.search("consent")
    assert not SENT_FOLDER.search("dissent")


def test_folder_of_reads_the_maildir_layout():
    root = Path("/m")
    assert folder_of(root, Path("/m/cur/123")) == "INBOX"
    assert folder_of(root, Path("/m/.Archives.2013/cur/123")) == ".Archives.2013"


def test_date_comes_from_the_header_when_sane():
    m = simple(date="Tue, 12 Mar 2013 09:15:00 +0100")
    _raw, parsed, source = when(m, Path("1362000000.x"), "INBOX")
    assert source == "header" and parsed.startswith("2013-03-12")


def test_date_falls_back_to_the_filename_epoch():
    m = simple(date="not a date at all")
    _raw, parsed, source = when(m, Path("1362000000.x"), "INBOX")
    assert source == "filename" and parsed.startswith("2013-")


def test_date_falls_back_to_the_folder_year():
    m = simple(date="rubbish")
    _raw, parsed, source = when(m, Path("nonnumeric.x"), ".Archives.2007")
    assert source == "folder" and parsed.startswith("2007-")


def test_absurd_header_dates_are_rejected():
    """A 1970 date is an epoch-0 artefact, not mail from 1970."""
    m = simple(date="Thu, 1 Jan 1970 00:00:00 +0000")
    _, _, source = when(m, Path("1362000000.x"), "INBOX")
    assert source != "header"


def test_no_date_anywhere_is_recorded_as_none():
    m = simple(date="rubbish")
    _raw, parsed, source = when(m, Path("nonnumeric.x"), ".somefolder")
    assert parsed is None and source == "none"
