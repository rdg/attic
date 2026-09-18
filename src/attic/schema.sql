-- Attachment index over a Maildir tree. See README.md.
--
-- Three tables so that dedup is structural, not a query you re-run:
--   blobs        one row per distinct byte-sequence, content-addressed on disk
--   attachments  one row per *occurrence* of a blob in a message part
--   messages     one row per message file, keyed on its maildir path

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    maildir_path    TEXT NOT NULL UNIQUE,  -- absolute path to the message file
    folder          TEXT NOT NULL,         -- raw maildir folder, e.g. .Archives.2015.Inbox.Sent
    mtime           REAL NOT NULL,         -- with size, lets a re-run skip unchanged files
    size            INTEGER NOT NULL,
    message_id      TEXT,                  -- Message-ID header, unreliable on old mail
    from_addr       TEXT,
    from_raw        TEXT,
    to_raw          TEXT,
    cc_raw          TEXT,
    subject         TEXT,
    date_raw        TEXT,                  -- the Date: header verbatim
    date_parsed     TEXT,                  -- ISO8601, NULL when unparseable
    date_source     TEXT,                  -- header | filename | folder | none
    direction       TEXT,                  -- in | out | unknown; derived, re-derivable
    parse_error     TEXT                   -- non-NULL when the message parsed badly
);

CREATE INDEX IF NOT EXISTS idx_messages_folder    ON messages(folder);
CREATE INDEX IF NOT EXISTS idx_messages_direction ON messages(direction);
CREATE INDEX IF NOT EXISTS idx_messages_date      ON messages(date_parsed);
CREATE INDEX IF NOT EXISTS idx_messages_from      ON messages(from_addr);

CREATE TABLE IF NOT EXISTS blobs (
    sha256          TEXT PRIMARY KEY,
    size            INTEGER NOT NULL,
    blob_path       TEXT NOT NULL,         -- relative to the blob root: ab/abcdef...
    sniffed_type    TEXT,                  -- from magic bytes, ignores what the mail claims
    sniffed_ext     TEXT
);

CREATE INDEX IF NOT EXISTS idx_blobs_type ON blobs(sniffed_type);
CREATE INDEX IF NOT EXISTS idx_blobs_size ON blobs(size);

CREATE TABLE IF NOT EXISTS attachments (
    id              INTEGER PRIMARY KEY,
    message_id_fk   INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    sha256          TEXT NOT NULL REFERENCES blobs(sha256),
    part_path       TEXT NOT NULL,         -- position in the MIME tree, e.g. 1.2.3
    filename        TEXT,                  -- decoded; DB only, never used as a disk path
    filename_raw    TEXT,                  -- the header before decoding
    mime_declared   TEXT,                  -- what the mail claims; often wrong
    disposition     TEXT,                  -- attachment | inline | NULL
    content_id      TEXT,
    decode_failed   INTEGER NOT NULL DEFAULT 0,  -- 1 = stored raw, base64 was broken
    UNIQUE (message_id_fk, part_path)
);

CREATE INDEX IF NOT EXISTS idx_att_sha  ON attachments(sha256);
CREATE INDEX IF NOT EXISTS idx_att_msg  ON attachments(message_id_fk);
CREATE INDEX IF NOT EXISTS idx_att_mime ON attachments(mime_declared);
CREATE INDEX IF NOT EXISTS idx_att_name ON attachments(filename);

-- Every occurrence joined to its message and blob. The viewer reads this.
CREATE VIEW IF NOT EXISTS v_attachments AS
SELECT a.id, a.sha256, a.filename, a.mime_declared, a.disposition,
       a.part_path, a.decode_failed,
       b.size, b.blob_path, b.sniffed_type, b.sniffed_ext,
       m.folder, m.direction, m.subject, m.from_addr, m.from_raw, m.to_raw,
       m.date_parsed, m.maildir_path,
       substr(m.date_parsed, 1, 4) AS year
FROM attachments a
JOIN blobs    b ON b.sha256 = a.sha256
JOIN messages m ON m.id = a.message_id_fk;
