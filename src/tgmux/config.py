"""tgmux configuration (TOML).

Default location: ``~/.config/tgmux/config.toml`` (override with ``--config``
or ``TGMUX_CONFIG``)::

    socket = "~/.local/state/tgmux/gateway.sock"
    db     = "~/.local/state/tgmux/tgmux.db"

    [telegram]
    session     = "~/Dev/barabot/config/scraper.session"  # authorized Telethon session
    credentials = "~/Dev/barabot/config/scraper.env"      # API_ID=... / API_HASH=...

    [reporter]                                            # optional service bot
    token_file = "~/.config/tgmux/reporter.token"         # BotFather token, file mode 0600
    chat_id    = 123456789                                # required with token_file: the only recipient

Everything can be written without prompts: ``tgmux telegram set`` and
``tgmux reporter set`` (see ``tgmux --help``).
"""
from __future__ import annotations

import json
import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".config/tgmux"
STATE_DIR = Path.home() / ".local/state/tgmux"
DEFAULT_CONFIG = CONFIG_DIR / "config.toml"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Reporter:
    token_file: Path
    chat_id: int

    def token(self) -> str:
        try:
            token = self.token_file.read_text().strip()
        except FileNotFoundError:
            raise ConfigError(f"reporter token file not found: {self.token_file}") from None
        if not token:
            raise ConfigError(f"reporter token file is empty: {self.token_file}")
        return token


@dataclass(frozen=True)
class Config:
    path: Path
    socket: Path
    db: Path
    state_dir: Path
    session: Path | None
    credentials: Path | None
    reporter: Reporter | None

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "gateway.lock"


def _path(value: Any) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"expected a path string, got {value!r}")
    return Path(value).expanduser()


def _reporter(section: dict) -> Reporter | None:
    token_file, chat_id = section.get("token_file"), section.get("chat_id")
    if token_file is None and chat_id is None:
        return None
    if token_file is None:
        raise ConfigError("[reporter].chat_id is set but [reporter].token_file is missing")
    if chat_id is None:
        raise ConfigError("[reporter].chat_id is required when [reporter].token_file is set")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        raise ConfigError(f"[reporter].chat_id must be an integer, got {chat_id!r}")
    return Reporter(_path(token_file), chat_id)


def read_raw(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def parse(path: Path, data: dict) -> Config:
    state_dir = _path(data.get("state_dir")) or STATE_DIR
    telegram = data.get("telegram", {})
    return Config(
        path=path,
        socket=_path(data.get("socket")) or state_dir / "gateway.sock",
        db=_path(data.get("db")) or state_dir / "tgmux.db",
        state_dir=state_dir,
        session=_path(telegram.get("session")),
        credentials=_path(telegram.get("credentials")),
        reporter=_reporter(data.get("reporter", {})),
    )


def config_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get("TGMUX_CONFIG") or DEFAULT_CONFIG).expanduser()


def load(path: str | Path | None = None) -> Config:
    path = config_path(path)
    return parse(path, read_raw(path))


# --- writing -----------------------------------------------------------------

def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (str, Path)):
        return json.dumps(str(value), ensure_ascii=False)  # a JSON string is a valid TOML basic string
    raise TypeError(f"cannot write {type(value).__name__} to config")


def dump(data: dict) -> str:
    lines = [f"{key} = {_value(value)}" for key, value in data.items() if not isinstance(value, dict)]
    for table, values in data.items():
        if isinstance(values, dict) and values:
            lines += ["", f"[{table}]"] + [f"{key} = {_value(value)}" for key, value in values.items()]
    return "\n".join(lines).lstrip("\n") + "\n"


def write_private(path: Path, content: str) -> None:
    """Atomically write a 0600 file (config, token)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update(path: Path, table: str, values: dict | None) -> Config:
    """Replace one [table] (None removes it), validate, then write."""
    data = read_raw(path)
    if values is None:
        data.pop(table, None)
    else:
        data[table] = values
    config = parse(path, data)  # refuse to write an invalid config
    write_private(path, dump(data))
    return config


# --- secrets -----------------------------------------------------------------

def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def api_credentials(cfg: Config) -> tuple[int, str]:
    values: dict[str, str] = {}
    if cfg.credentials is not None:
        values.update(read_env_file(cfg.credentials))
    api_id = os.environ.get("TGMUX_API_ID") or values.get("API_ID")
    api_hash = os.environ.get("TGMUX_API_HASH") or values.get("API_HASH")
    if not api_id or not api_hash:
        raise ConfigError("API_ID/API_HASH not found: set [telegram].credentials or TGMUX_API_ID/TGMUX_API_HASH")
    return int(api_id), api_hash
