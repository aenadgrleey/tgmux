"""Service reports: things tgmux dropped or did not expect.

Reports go to a separate Telegram bot (Bot API over HTTPS), never through the
shared user account. Repeats of the same key are folded into one message per
``window`` seconds so a noisy chat cannot flood the bot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

MAX_TEXT = 3500


class Reporter(Protocol):
    def report(self, key: str, text: str, *, window: float | None = None) -> None: ...


class LogReporter:
    """Fallback when no bot is configured: reports only go to the log."""

    def report(self, key: str, text: str, *, window: float | None = None) -> None:
        log.warning("report [%s]: %s", key, text)


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
        """Return (send now?, how many repeats were folded since the last send)."""
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


class BotReporter:
    """Sends reports to the one configured peer via a BotFather bot. Never blocks the caller."""

    def __init__(self, token: str, chat_id: int, *, window: float = 3600, queue_size: int = 500) -> None:
        self._token = token
        self._chat_id = chat_id
        self._dedup = Deduplicator(window)
        self._queue: asyncio.Queue[str] = asyncio.Queue(queue_size)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def report(self, key: str, text: str, *, window: float | None = None) -> None:
        log.warning("report [%s]: %s", key, text)
        send, folded = self._dedup.admit(key, window)
        if not send:
            return
        if folded:
            text += f"\n\n(+{folded} повторов за прошлый период)"
        try:
            self._queue.put_nowait(text[:MAX_TEXT])
        except asyncio.QueueFull:
            log.error("report queue full, dropping report %s", key)

    async def _run(self) -> None:
        while True:
            text = await self._queue.get()
            try:
                await asyncio.to_thread(send_bot_message, self._token, self._chat_id, "tgmux: " + text)
            except Exception:  # reporting must never take the gateway down
                log.exception("failed to deliver report to the service bot")
                await asyncio.sleep(5)


class BotApiError(RuntimeError):
    pass


def send_bot_message(token: str, chat_id: int, text: str) -> None:
    body = json.dumps({"chat_id": chat_id, "text": text}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        try:
            description = json.load(exc).get("description", str(exc))
        except ValueError:
            description = str(exc)
        raise BotApiError(description) from None
