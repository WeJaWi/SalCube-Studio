"""Reads what the macOS Spotify desktop app is playing, via AppleScript.

No credentials and no network: the desktop app answers these queries
locally. Only the resulting episode id is looked up against the Web API.

Two details this module exists to contain:

* `tell application "Spotify"` LAUNCHES Spotify when it is not running, so
  every read is gated on a process check first. A stats companion must never
  be the reason the music starts.
* AppleScript renders real numbers in the system locale — `player position`
  comes back as "103,189" on a comma-decimal Mac. Both numbers are therefore
  reduced to integers with `div 1` inside AppleScript, which is always plain
  digits, rather than parsed as floats on the Python side.
"""
from __future__ import annotations

import subprocess
from typing import NamedTuple, Optional

SEP = "\x1f"

# One round trip per poll: player state, what is loaded, and where we are.
SCRIPT = """
set sep to character id 31
tell application "Spotify"
  set t to current track
  return (player state as text) & sep & (spotify url of t) & sep & ¬
    (name of t) & sep & (artist of t) & sep & (album of t) & sep & ¬
    (((duration of t) div 1) as text) & sep & (((player position) div 1) as text)
end tell
"""


class NowPlaying(NamedTuple):
    running: bool = False
    state: str = ""           # playing | paused | stopped
    uri: str = ""
    kind: str = ""            # episode | track | local | ""
    item_id: str = ""
    title: str = ""
    artist: str = ""          # for podcasts this is the show name
    album: str = ""
    duration_ms: int = 0
    position_s: int = 0
    error: Optional[str] = None

    @property
    def is_podcast(self) -> bool:
        return self.kind == "episode" and bool(self.item_id)


def spotify_running() -> bool:
    """True when a Spotify process exists — checked before any AppleScript."""
    try:
        return subprocess.run(["pgrep", "-x", "Spotify"],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def _split_uri(uri: str) -> tuple:
    parts = uri.split(":")
    if len(parts) >= 3 and parts[0] == "spotify":
        return parts[1], parts[-1]
    return "", ""


def _to_int(value: str) -> int:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return 0


def read(timeout: int = 5) -> NowPlaying:
    """Snapshot of the desktop player, or a NowPlaying carrying the reason why not."""
    if not spotify_running():
        return NowPlaying(running=False)
    try:
        result = subprocess.run(["osascript", "-"], input=SCRIPT, text=True,
                                capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return NowPlaying(running=True, error="Spotify did not answer in time")
    except OSError as exc:
        return NowPlaying(running=True, error=f"could not run osascript: {exc}")

    if result.returncode != 0:
        message = (result.stderr or "").strip().splitlines()
        detail = message[-1] if message else "unknown AppleScript error"
        if "Not authorized" in detail or "-1743" in detail:
            detail = ("macOS has not granted this terminal permission to control "
                      "Spotify. Approve it under System Settings → Privacy & "
                      "Security → Automation.")
        elif "current track" in detail:
            detail = "Spotify has nothing loaded yet — press play once."
        return NowPlaying(running=True, error=detail)

    fields = result.stdout.rstrip("\n").split(SEP)
    if len(fields) < 7:
        return NowPlaying(running=True, error="unexpected reply from Spotify")

    kind, item_id = _split_uri(fields[1])
    return NowPlaying(
        running=True,
        state=fields[0].strip(),
        uri=fields[1].strip(),
        kind=kind,
        item_id=item_id,
        title=fields[2],
        artist=fields[3],
        album=fields[4],
        duration_ms=_to_int(fields[5]),
        position_s=_to_int(fields[6]),
    )
