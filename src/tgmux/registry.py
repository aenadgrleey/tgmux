"""SQLite registry: integrations and which integration owns which chat.

``chat_id`` is Telethon's marked peer id (users > 0, basic groups < 0,
channels/supergroups -100...). It is the primary key, so a chat has at most
one owner and first-come claiming is a single atomic statement.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS integrations (
    name        TEXT PRIMARY KEY,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_bindings (
    chat_id     INTEGER PRIMARY KEY,
    integration TEXT NOT NULL REFERENCES integrations(name) ON DELETE CASCADE,
    source      TEXT NOT NULL,           -- join | invite | dm | admin
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_bindings_by_integration ON chat_bindings(integration);
"""


@dataclass(frozen=True)
class Binding:
    chat_id: int
    integration: str
    source: str
    created_at: int


class UnknownIntegrationError(KeyError):
    pass


class Registry:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            Path(path).touch(mode=0o600, exist_ok=True)
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # --- integrations -----------------------------------------------------

    def add_integration(self, name: str) -> None:
        """Register an integration name. Only registered names may connect."""
        if not NAME_RE.fullmatch(name):
            raise ValueError("integration name must match [a-z0-9][a-z0-9_-]{0,31}")
        try:
            self._db.execute(
                "INSERT INTO integrations(name, created_at) VALUES (?, ?)", (name, int(time.time()))
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"integration {name!r} already exists") from exc

    def remove_integration(self, name: str) -> bool:
        """Remove an integration; its chats become free."""
        return self._db.execute("DELETE FROM integrations WHERE name = ?", (name,)).rowcount > 0

    def integrations(self) -> list[str]:
        return [row[0] for row in self._db.execute("SELECT name FROM integrations ORDER BY name")]

    def has_integration(self, name: str) -> bool:
        return self._db.execute("SELECT 1 FROM integrations WHERE name = ?", (name,)).fetchone() is not None

    # --- ownership --------------------------------------------------------

    def owner(self, chat_id: int) -> str | None:
        row = self._db.execute(
            "SELECT integration FROM chat_bindings WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row[0] if row else None

    def claim(self, chat_id: int, integration: str, source: str) -> str:
        """Take a free chat. Returns the owner after the call (may be someone else)."""
        row = self._db.execute(
            "INSERT INTO chat_bindings(chat_id, integration, source, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO NOTHING RETURNING integration",
            (chat_id, integration, source, int(time.time())),
        ).fetchone()
        if row:
            return row[0]
        owner = self.owner(chat_id)
        assert owner is not None
        return owner

    def assign(self, chat_id: int, integration: str, source: str = "admin") -> str | None:
        """Force ownership (admin action). Returns the previous owner."""
        if not self.has_integration(integration):
            raise UnknownIntegrationError(integration)
        previous = self.owner(chat_id)
        self._db.execute(
            "INSERT INTO chat_bindings(chat_id, integration, source, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET integration = excluded.integration, "
            "source = excluded.source, created_at = excluded.created_at",
            (chat_id, integration, source, int(time.time())),
        )
        return previous

    def release(self, chat_id: int, integration: str | None = None) -> bool:
        if integration is None:
            cur = self._db.execute("DELETE FROM chat_bindings WHERE chat_id = ?", (chat_id,))
        else:
            cur = self._db.execute(
                "DELETE FROM chat_bindings WHERE chat_id = ? AND integration = ?", (chat_id, integration)
            )
        return cur.rowcount > 0

    def owned_by(self, integration: str) -> list[int]:
        return [
            row[0]
            for row in self._db.execute(
                "SELECT chat_id FROM chat_bindings WHERE integration = ? ORDER BY created_at", (integration,)
            )
        ]

    def bindings(self) -> list[Binding]:
        return [
            Binding(*row)
            for row in self._db.execute(
                "SELECT chat_id, integration, source, created_at FROM chat_bindings ORDER BY integration, created_at"
            )
        ]
