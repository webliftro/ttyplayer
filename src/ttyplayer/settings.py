"""The user's preferences: one flat TOML file of key = scalar, every key optional."""

import dataclasses
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ttyplayer.utils import APP_NAME, WINDOWS

SEARCH_LIMIT_RANGE = range(1, 51)
SERVER_PORT_RANGE = range(1, 65536)


class SettingsError(Exception):
    """The settings file or a value for it is not what ttyplayer understands."""


@dataclass(frozen=True)
class Settings:
    show_clock: bool = True
    theme: str = "textual-dark"
    search_limit: int = 10
    server_host: str = "127.0.0.1"
    server_port: int = 7700
    server_token: str = ""  # ttyplayer serve generates it on first use
    remote_url: str = ""  # the server tui drives instead of its own player, e.g. http://host:7700


DEFAULTS = Settings()
KEYS = [field.name for field in dataclasses.fields(Settings)]
VALID_KEYS = f"valid keys: {', '.join(KEYS)}"


def settings_path() -> Path:
    """$XDG_CONFIG_HOME/ttyplayer/settings.toml, by default ~/.config/ttyplayer/,
    or %APPDATA%\\ttyplayer\\ on Windows."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or default_config_home()) / APP_NAME / "settings.toml"


def default_config_home():
    if WINDOWS:
        return os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    return Path.home() / ".config"


def load(path: Path | None = None) -> Settings:
    """The saved settings over the defaults; unknown keys are ignored."""
    path = path or settings_path()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return DEFAULTS
    except OSError as error:
        raise SettingsError(f"Cannot read {path}: {error.strerror or error}") from error
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise SettingsError(f"{path} is not valid TOML: {error}") from error
    values = {key: raw[key] for key in KEYS if key in raw}
    for key, value in values.items():
        check(key, value)
    return Settings(**values)


def save(settings: Settings, path: Path | None = None):
    path = path or settings_path()
    lines = [f"{key} = {toml_value(getattr(settings, key))}\n" for key in KEYS]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(lines), encoding="utf-8")
    except OSError as error:
        raise SettingsError(f"Cannot write {path}: {error.strerror or error}") from error


def update(key: str, raw_value: str, path: Path | None = None) -> Settings:
    """The saved settings with key set from the text raw_value, saved."""
    check_key(key)
    return change(key, parse(key, raw_value), path)


def change(key: str, value, path: Path | None = None) -> Settings:
    """The saved settings with key set to value, saved; the file is read again so other keys keep their saved values."""
    settings = dataclasses.replace(load(path), **{key: value})
    save(settings, path)
    return settings


def check_key(key):
    if key not in KEYS:
        raise SettingsError(f"Unknown setting {key!r}; {VALID_KEYS}")


def parse(key, raw_value):
    """raw_value (as typed on the command line) as the type key holds."""
    kind = type(getattr(DEFAULTS, key))
    text = raw_value.strip()
    if kind is bool:
        if text.lower() not in ("true", "false"):
            raise SettingsError(f"{key} must be true or false, not {raw_value!r}; {VALID_KEYS}")
        value = text.lower() == "true"
    elif kind is int:
        try:
            value = int(text)
        except ValueError:
            raise SettingsError(f"{key} must be a whole number, not {raw_value!r}; {VALID_KEYS}") from None
    else:
        value = raw_value
    check(key, value)
    return value


def check(key, value):
    """A SettingsError unless value has key's type (bool is not an int here) and is in range."""
    kind = type(getattr(DEFAULTS, key))
    if type(value) is not kind:
        raise SettingsError(f"{key} must be {kind.__name__}, not {value!r}")
    if key == "search_limit" and value not in SEARCH_LIMIT_RANGE:
        raise SettingsError(
            f"search_limit must be between {SEARCH_LIMIT_RANGE.start} and {SEARCH_LIMIT_RANGE.stop - 1}, not {value}"
        )
    if key == "server_port" and value not in SERVER_PORT_RANGE:
        raise SettingsError(
            f"server_port must be between {SERVER_PORT_RANGE.start} and {SERVER_PORT_RANGE.stop - 1}, not {value}"
        )


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return toml_string(value)


def toml_string(text):
    """A TOML basic string: backslash, quote and control characters escaped."""
    escaped = "".join(
        f"\\u{ord(char):04x}" if ord(char) < 0x20 or ord(char) == 0x7F else char
        for char in text.replace("\\", "\\\\").replace('"', '\\"')
    )
    return f'"{escaped}"'


def display(value):
    """value as config set takes it: true/false, 10, textual-dark."""
    return toml_value(value) if isinstance(value, bool) else str(value)
