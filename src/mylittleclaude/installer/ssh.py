"""SSH-key local-side validation. No actual SSH dialing.

Per spec §2.3 step 5: validate the key file exists and has mode 600 or 400.
Offer to chmod if too open. We never call ssh from the installer — first-use
SSH happens when the bot tries to invoke a remote instance.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass
class KeyCheck:
    path: Path
    exists: bool
    is_file: bool
    mode: int                 # last 9 bits of st_mode
    is_too_open: bool         # True if not 600 / 400 / 700-anything-narrower
    error: str | None = None  # human-readable problem, or None


def check_key(path_str: str) -> KeyCheck:
    p = Path(path_str)
    try:
        st = p.stat()
    except FileNotFoundError:
        return KeyCheck(path=p, exists=False, is_file=False, mode=0, is_too_open=False,
                        error="no such file")
    except OSError as e:
        return KeyCheck(path=p, exists=True, is_file=False, mode=0, is_too_open=False,
                        error=f"stat failed: {e}")

    if not stat.S_ISREG(st.st_mode):
        return KeyCheck(path=p, exists=True, is_file=False, mode=0, is_too_open=False,
                        error="not a regular file")

    mode = st.st_mode & 0o777
    too_open = mode not in (0o600, 0o400)
    return KeyCheck(
        path=p, exists=True, is_file=True, mode=mode, is_too_open=too_open,
        error=None,
    )


def tighten(path: Path) -> None:
    """Apply mode 600. Raises OSError on failure."""
    os.chmod(path, 0o600)
