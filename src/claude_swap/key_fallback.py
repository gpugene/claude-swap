"""Metered API-key fallback that running Claude Code sessions actually follow.

Switching the live login onto an API-key account does not work for sessions
that are already running: Claude Code memoizes its managed-key lookup at
startup (a ``null`` result included), so a session that started on an OAuth
login never sees a key written later. It keeps its in-memory OAuth token until
that token stops working, re-reads the keychain, finds no OAuth login, and
fails every request with "Not logged in · Please run /login". Measured
2026-09-28: four sessions died that way 70-90 s after a switch onto the key.

``apiKeyHelper`` is the one auth source Claude Code re-reads from settings on
every request, so adding it moves every running session (and its subagents)
onto the key within a second, and removing it moves them straight back to the
OAuth login, which cswap keeps on a subscription account the whole time.
Verified 2026-09-28 with a running ``claude -p --input-format stream-json``
process: the helper was invoked on the very next request after it was added,
and the in-flight retry succeeded on OAuth once it was removed.

The helper command is ``cswap key-helper``, which prints the key of the
lowest-numbered API-key account at call time; the key itself is never written
into Claude Code's settings.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

from claude_swap.paths import get_backup_root

HELPER_SUBCOMMAND = "key-helper"
HOLD_FILENAME = "key-fallback.hold"


def claude_settings_path() -> Path:
    """Claude Code's user settings file (``$CLAUDE_CONFIG_DIR`` aware).

    Resolved through symlinks: ``cswap run`` profiles link their
    ``settings.json`` to this file, and writing through the real path keeps
    the links intact.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return (Path(base).expanduser() / "settings.json").resolve()


def helper_command() -> str:
    """Absolute ``cswap key-helper`` command line for ``apiKeyHelper``."""
    exe = shutil.which("cswap") or os.path.abspath(sys.argv[0])
    return f"{exe} {HELPER_SUBCOMMAND}"


def hold_path() -> Path:
    """The hold file: ``<backup root>/key-fallback.hold``."""
    return get_backup_root() / HOLD_FILENAME


def held(now: float | None = None, path: Path | None = None) -> bool:
    """True while a hold file keeps the fallback on.

    A running session keeps the plan whose token it holds for hours: on
    2026-10-26 sessions still hit plan 3's limit 24 minutes after the live
    login moved to plan 4. Moving the live login cannot reach them; only the
    key can. The session sentinel writes this file when sessions are stuck on
    a spent plan, so the engine does not take the key away the moment the live
    plan has room. JSON ``{"until": <epoch>}`` holds until then; anything else
    (a note written by hand) holds until the file is removed.
    """
    p = path or hold_path()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        data = json.loads(text)
    except ValueError:
        return True
    if isinstance(data, dict) and isinstance(data.get("until"), (int, float)):
        return (time.time() if now is None else now) < data["until"]
    return True


def is_ours(value: object) -> bool:
    return isinstance(value, str) and value.rstrip().endswith(
        f"cswap {HELPER_SUBCOMMAND}"
    )


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def state(path: Path | None = None) -> str:
    """``"on"`` (our helper is set), ``"off"``, ``"foreign"`` (someone else's
    apiKeyHelper is set; never touched) or ``"unreadable"``."""
    data = _read(path or claude_settings_path())
    if data is None:
        return "unreadable"
    value = data.get("apiKeyHelper")
    if value is None:
        return "off"
    return "on" if is_ours(value) else "foreign"


def set_enabled(on: bool, path: Path | None = None) -> bool:
    """Add or remove our ``apiKeyHelper``. Returns True when the file changed.

    Written in place (truncate + one write), not via rename: Claude Code
    watches this file, and a rename swaps the inode out from under the
    watcher. A foreign ``apiKeyHelper`` is never overwritten or removed.
    """
    path = path or claude_settings_path()
    data = _read(path)
    if data is None:
        return False
    current = data.get("apiKeyHelper")
    if current is not None and not is_ours(current):
        return False
    if on:
        wanted = helper_command()
        if current == wanted:
            return False
        data["apiKeyHelper"] = wanted
    else:
        if current is None:
            return False
        del data["apiKeyHelper"]
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    with open(path, "r+", encoding="utf-8") as f:
        f.seek(0)
        f.write(text)
        f.truncate()
    return True
