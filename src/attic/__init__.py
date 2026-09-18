"""attic — make twenty years of mail attachments browsable.

Two commands. `attic extract` walks a Maildir tree, pulls every attachment
out, stores each distinct file once under its SHA-256, and records where it
came from in sqlite. `attic serve` is a local, filterable browser over that
index.
"""

__version__ = "0.1.0"
