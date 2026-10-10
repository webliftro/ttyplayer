import dataclasses
import pathlib
import re
import subprocess
import sys
import tomllib

import pytest

from conftest import posix_only
from ttyplayer import settings
from ttyplayer.settings import Settings, SettingsError



def test_settings_fields_and_defaults():
    assert [(field.name, field.default) for field in dataclasses.fields(Settings)] == [
        ("show_clock", True), ("theme", "textual-dark"), ("search_limit", 10),
        ("server_host", "127.0.0.1"), ("server_port", 7700), ("server_token", ""),
        ("remote_url", ""), ("stream_enabled", False), ("spotify_client_id", ""), ("show_levels", True), ("show_art", True), ("show_lyrics", True), ("search_source", "youtube"),
        ("radio", False), ("normalize_loudness", False), ("prefetch", True), ("seek_seconds", 5), ("volume_step", 5),
    ]
    assert settings.KEYS == [
        "show_clock", "theme", "search_limit", "server_host", "server_port", "server_token", "remote_url", "stream_enabled",
        "spotify_client_id", "show_levels", "show_art", "show_lyrics", "search_source", "radio", "normalize_loudness", "prefetch", "seek_seconds", "volume_step",
    ]


def test_load_without_a_file_is_the_defaults(tmp_path):
    assert settings.load(tmp_path / "missing.toml") == Settings()


def test_save_writes_a_flat_toml_that_loads_back_identically(tmp_path):
    path = tmp_path / "deep" / "settings.toml"
    saved = Settings(show_clock=False, theme='odd "theme" \\ with\ttab', search_limit=42)
    settings.save(saved, path)
    assert tomllib.loads(path.read_text(encoding="utf-8")) == dataclasses.asdict(saved)
    assert settings.load(path) == saved
    assert path.read_text(encoding="utf-8").splitlines()[0] == "show_clock = false"


def test_load_ignores_unknown_keys_and_fills_missing_ones(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('theme = "nord"\nvolume = 3\n', encoding="utf-8")
    assert settings.load(path) == Settings(theme="nord")


@pytest.mark.parametrize(
    "text, message",
    [
        ("show_clock = [", "is not valid TOML"),
        ('show_clock = "no"', "show_clock must be bool"),
        ("search_limit = true", "search_limit must be int"),
        ("theme = 3", "theme must be str"),
        ("search_limit = 0", "search_limit must be between 1 and 50"),
        ("normalize_loudness = 1", "normalize_loudness must be bool"),
        ("seek_seconds = 0", "seek_seconds must be between 1 and 300"),
        ("volume_step = 51", "volume_step must be between 1 and 50"),
    ],
)
def test_load_rejects_a_malformed_file_or_a_wrong_type_in_one_line(tmp_path, text, message):
    path = tmp_path / "settings.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SettingsError, match=message) as caught:
        settings.load(path)
    assert "\n" not in str(caught.value)


def test_update_coerces_text_saves_and_keeps_the_other_keys(tmp_path):
    path = tmp_path / "settings.toml"
    settings.save(Settings(theme="nord"), path)
    assert settings.update("show_clock", "False", path) == Settings(show_clock=False, theme="nord")
    assert settings.update("search_limit", " 25 ", path).search_limit == 25
    assert settings.update("theme", "gruvbox", path).theme == "gruvbox"
    assert settings.load(path) == Settings(show_clock=False, theme="gruvbox", search_limit=25)


def test_update_with_an_unknown_key_names_the_valid_keys(tmp_path):
    with pytest.raises(SettingsError, match="Unknown setting 'clock'; valid keys: show_clock, theme, search_limit, server_host, server_port, server_token, remote_url"):
        settings.update("clock", "true", tmp_path / "settings.toml")
    assert not (tmp_path / "settings.toml").exists()


@pytest.mark.parametrize(
    "key, value, message",
    [
        ("show_clock", "yes", "show_clock must be true or false, not 'yes'; valid keys: show_clock, theme, search_limit, server_host, server_port, server_token, remote_url"),
        ("search_limit", "ten", "search_limit must be a whole number, not 'ten'; valid keys: show_clock, theme, search_limit, server_host, server_port, server_token, remote_url"),
        ("search_limit", "0", "search_limit must be between 1 and 50, not 0"),
        ("search_limit", "51", "search_limit must be between 1 and 50, not 51"),
        ("server_port", "0", "server_port must be between 1 and 65535, not 0"),
        ("server_port", "65536", "server_port must be between 1 and 65535, not 65536"),
        ("normalize_loudness", "on", "normalize_loudness must be true or false, not 'on'"),
        ("seek_seconds", "0", "seek_seconds must be between 1 and 300, not 0"),
        ("seek_seconds", "301", "seek_seconds must be between 1 and 300, not 301"),
        ("seek_seconds", "2.5", "seek_seconds must be a whole number, not '2.5'"),
        ("volume_step", "0", "volume_step must be between 1 and 50, not 0"),
        ("volume_step", "51", "volume_step must be between 1 and 50, not 51"),
        ("search_source", "bandcamp", "search_source must be one of youtube, soundcloud, podcast, not 'bandcamp'"),
    ],
)
def test_update_rejects_a_bad_value_and_saves_nothing(tmp_path, key, value, message):
    path = tmp_path / "settings.toml"
    with pytest.raises(SettingsError, match=message):
        settings.update(key, value, path)
    assert not path.exists()


def test_load_turns_a_read_error_into_one_line(tmp_path):
    with pytest.raises(SettingsError, match=f"Cannot read {re.escape(str(tmp_path))}") as caught:
        settings.load(tmp_path)
    assert "\n" not in str(caught.value)


def test_save_turns_a_write_error_into_one_line(tmp_path):
    (tmp_path / "file").write_text("", encoding="utf-8")
    path = tmp_path / "file" / "settings.toml"
    with pytest.raises(SettingsError, match="Cannot write") as caught:
        settings.save(Settings(), path)
    assert "\n" not in str(caught.value)


@pytest.mark.parametrize("limit", ["1", "50"])
def test_search_limit_range_is_inclusive(tmp_path, limit):
    assert settings.update("search_limit", limit, tmp_path / "s.toml").search_limit == int(limit)


@pytest.mark.parametrize("key, value", [("seek_seconds", "1"), ("seek_seconds", "300"), ("volume_step", "1"), ("volume_step", "50")])
def test_seek_seconds_and_volume_step_ranges_are_inclusive(tmp_path, key, value):
    assert getattr(settings.update(key, value, tmp_path / "s.toml"), key) == int(value)


def test_normalize_loudness_saves_and_loads_back(tmp_path):
    path = tmp_path / "s.toml"
    assert settings.update("normalize_loudness", "true", path).normalize_loudness is True
    assert settings.load(path) == Settings(normalize_loudness=True)


@posix_only
def test_settings_path_defaults_to_dot_config_on_posix(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "WINDOWS", False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert settings.settings_path() == tmp_path / ".config" / "ttyplayer" / "settings.toml"


def test_settings_path_follows_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert settings.settings_path() == tmp_path / "ttyplayer" / "settings.toml"


def test_settings_path_is_appdata_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "WINDOWS", True)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert settings.settings_path() == tmp_path / "ttyplayer" / "settings.toml"


def test_the_player_does_not_import_settings():
    code = "import sys, ttyplayer.player; print('ttyplayer.settings' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout == "False\n"


def test_readme_names_every_setting_with_its_default():
    readme = (pathlib.Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Settings\n", 1)[1].split("\n## ", 1)[0]
    for key in settings.KEYS:
        default = settings.display(getattr(settings.DEFAULTS, key))
        empty = "*generated*" if key == "server_token" else "*none*"
        assert f"| `{key}` | {f'`{default}`' if default else empty} |" in section


def test_search_source_takes_every_source(tmp_path):
    from ttyplayer.youtube import SOURCES

    for source in SOURCES:
        assert settings.update("search_source", source, tmp_path / "s.toml").search_source == source
