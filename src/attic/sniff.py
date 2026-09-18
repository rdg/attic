"""Identify a byte sequence by its magic number.

What a mail part *claims* to be cannot be trusted: application/octet-stream
covers a multitude of PDFs, and an Illustrator file declares itself
PostScript while actually being a PDF. Everything downstream -- whether a
thumbnail can be made, whether the browser can render it -- keys off what
the bytes actually are.
"""

from __future__ import annotations

# Magic-byte sniffing. What a message *claims* a part is cannot be trusted --
# application/octet-stream covers a multitude of PDFs.
# fmt: off  -- the columns are the point; reflowed this is unreadable
MAGIC: list[tuple[bytes, str, str]] = [
    (b"%PDF-", "application/pdf", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
    (b"II*\x00", "image/tiff", "tif"),
    (b"MM\x00*", "image/tiff", "tif"),
    (b"BM", "image/bmp", "bmp"),
    (b"\x00\x00\x01\x00", "image/vnd.microsoft.icon", "ico"),
    (b"%!PS", "application/postscript", "ps"),
    (b"\x1f\x8b", "application/gzip", "gz"),
    (b"BZh", "application/x-bzip2", "bz2"),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed", "7z"),
    (b"Rar!\x1a\x07", "application/vnd.rar", "rar"),
    (b"\xd0\xcf\x11\xe0", "application/x-ole-storage", "doc"),  # legacy Office
    (b"{\\rtf", "application/rtf", "rtf"),
    (b"OggS", "audio/ogg", "ogg"),
    (b"ID3", "audio/mpeg", "mp3"),
    (b"fLaC", "audio/flac", "flac"),
    (b"\x00\x01\x00\x00\x00", "font/ttf", "ttf"),
    (b"OTTO", "font/otf", "otf"),
    (b"wOFF", "font/woff", "woff"),
    (b"\x7fELF", "application/x-executable", "elf"),
    (b"\xca\xfe\xba\xbe", "application/java-vm", "class"),
    (b"SQLite format 3\x00", "application/vnd.sqlite3", "sqlite"),
]
# fmt: on

# ZIP containers: OOXML, ODF and JARs are all zips with a marker inside.
ZIP_OOXML = {
    b"word/": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx",
    ),
    b"xl/": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    b"ppt/": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "pptx",
    ),
}


def sniff(data: bytes) -> tuple[str | None, str | None]:
    """Identify bytes by their magic number, ignoring the declared type."""
    head = data[:512]
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        window = data[:4096]
        if b"mimetypeapplication/vnd.oasis.opendocument" in window:
            return "application/vnd.oasis.opendocument", "odf"
        for marker, (mime, ext) in ZIP_OOXML.items():
            if marker in window:
                return mime, ext
        return "application/zip", "zip"
    if head[4:12] in (b"ftypisom", b"ftypmp42", b"ftypM4A ", b"ftypqt  "):
        return "video/mp4", "mp4"
    if head[:4] == b"RIFF":
        if head[8:12] == b"WAVE":
            return "audio/wav", "wav"
        if head[8:12] == b"WEBP":
            return "image/webp", "webp"
        if head[8:12] == b"AVI ":
            return "video/x-msvideo", "avi"
    for magic, mime, ext in MAGIC:
        if head.startswith(magic):
            return mime, ext
    if head.lstrip()[:5].lower() == b"<?xml":
        return "application/xml", "xml"
    if head.lstrip()[:6].lower().startswith((b"<html", b"<!doct")):
        return "text/html", "html"
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return None, None
    return "text/plain", "txt"
