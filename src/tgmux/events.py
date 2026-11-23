"""Events: things tgmux dropped, did not expect, or wants an operator to notice.

tgmux only logs them. Where a log line ends up (journald, a chat bot, a file)
is the host's business, not tgmux's: on a server a journald forwarder picks
lines by priority. Levels follow syslog: ERROR (3) and WARNING (4) are
problems, NOTICE (5) is "normal but significant" (started, new integration,
back online).

Repeats of the same key are folded into one line per ``window`` seconds, so a
noisy chat cannot flood whatever reads the log.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Protocol

NOTICE = 25  # between INFO and WARNING, like syslog's notice
logging.addLevelName(NOTICE, "NOTICE")

DEFAULT_WINDOW = 3600

_NOTICE = ("lifecycle", "registered:", "online:")
_ERROR = ("protocol:", "bad-request:", "call-error:", "route-error:", "sweep-error", "upstream-lost")

log = logging.getLogger("tgmux.events")


def level_for(key: str) -> int:
    if key.startswith(_NOTICE):
        return NOTICE
    if key.startswith(_ERROR):
        return logging.ERROR
    return logging.WARNING


class EventSink(Protocol):
    def report(self, key: str, text: str, *, window: float | None = None) -> None: ...


@dataclass
class _Seen:
    first_sent: float
    window: float
    suppressed: int = 0


class Deduplicator:
    def __init__(self, window: float, clock=time.monotonic) -> None:
        self.window = window
        self._clock = clock
        self._seen: dict[str, _Seen] = {}

    def admit(self, key: str, window: float | None = None) -> tuple[bool, int]:
        """Return (emit now?, how many repeats were folded since the last emit)."""
        now = self._clock()
        seen = self._seen.get(key)
        if seen is None or now - seen.first_sent >= seen.window:
            folded = seen.suppressed if seen else 0
            self._seen[key] = _Seen(now, self.window if window is None else window)
            if len(self._seen) > 10_000:
                self._evict(now)
            return True, folded
        seen.suppressed += 1
        return False, 0

    def _evict(self, now: float) -> None:
        for key in [k for k, v in self._seen.items() if now - v.first_sent >= v.window]:
            del self._seen[key]


class EventLog:
    """Writes events to the ``tgmux.events`` logger at their syslog level, folding repeats."""

    def __init__(self, window: float = DEFAULT_WINDOW, clock=time.monotonic, logger: logging.Logger = log) -> None:
        self._dedup = Deduplicator(window, clock)
        self._log = logger

    def report(self, key: str, text: str, *, window: float | None = None) -> None:
        emit, folded = self._dedup.admit(key, window)
        if not emit:
            return
        if folded:
            text += f" (+{folded} повторов за прошлый период)"
        self._log.log(level_for(key), "%s", text, extra={"event_key": key})


# --- log format ------------------------------------------------------------------

_SYSLOG = {logging.CRITICAL: 2, logging.ERROR: 3, logging.WARNING: 4, NOTICE: 5, logging.INFO: 6, logging.DEBUG: 7}


class SyslogPrefixFormatter(logging.Formatter):
    """Under systemd, prefix lines with <N> so journald stores the right priority."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        level = max((lvl for lvl in _SYSLOG if lvl <= record.levelno), default=logging.DEBUG)
        prefix = f"<{_SYSLOG[level]}>"
        return "\n".join(prefix + line for line in text.splitlines() or [""])


def setup_logging(instance: str) -> None:
    under_journald = bool(os.environ.get("JOURNAL_STREAM"))
    handler = logging.StreamHandler()
    if under_journald:  # journald adds timestamps itself
        handler.setFormatter(SyslogPrefixFormatter(f"[{instance}] %(name)s: %(message)s"))
    else:
        handler.setFormatter(logging.Formatter(f"%(asctime)s %(levelname)s [{instance}] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("telethon").setLevel(logging.WARNING)
