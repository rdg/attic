"""The one exception this package raises deliberately.

Everything under src/ is importable as a library, so nothing below the CLI
calls sys.exit. Misuse raises AtticError and the CLI turns it into a message
and an exit code.
"""

from __future__ import annotations


class AtticError(Exception):
    """A problem the user can fix: a missing directory, an index not built."""
