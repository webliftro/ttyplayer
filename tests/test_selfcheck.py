import functools
import inspect
import io
import json
import threading
import urllib.error
import wave

import pytest
from typer.testing import CliRunner

from ttyplayer import art, cli, favorites, history, playlists, podcasts, positions, scrobble, selfcheck, settings, spotify
from ttyplayer import subscriptions
from ttyplayer.cli import app
from ttyplayer.lyrics import Lyrics
from ttyplayer.models import Video

runner = CliRunner()

SONG = Video(id="abc", title="Song", uploader="Band", duration=200, thumbnail="https://i.ytimg.com/vi/abc/hq.jpg")
SHOW = podcasts.Show(name="News Now", author="NPR", feed_url="https://feeds.example/news", artwork=None, id=1)


def answers(status, detail="fine"):
    return lambda quick: (status, detail)


def raises(quick):
    raise ValueError("no route to host\nsecond line")


# --- run ----------------------------------------------------------------


def test_run_keeps_each_status_and_detail_in_order():
    checks = [(status, answers(status, f"{status} detail")) for status in ("ok", "warn", "fail", "skip")]
    results = selfcheck.run(checks)
    assert [(result.name, result.status, result.detail) for result in results] == [
        ("ok", "ok", "ok detail"), ("warn", "warn", "warn detail"), ("fail", "fail", "fail detail"), ("skip", "skip", "skip detail"),
    ]
    assert all(isinstance(result.seconds, float) for result in results)


def test_run_passes_quick_to_every_check():
    seen = []
    selfcheck.run([("a", lambda quick: seen.append(quick) or ("ok", "")), ("b", lambda quick: seen.append(quick) or ("ok", ""))], quick=True)
    assert seen == [True, True]


def test_a_check_that_raises_fails_with_the_first_line_of_its_exception():
    [result] = selfcheck.run([("net", raises)])
    assert (result.status, result.detail) == ("fail", "no route to host")


def test_a_check_that_raises_without_a_message_fails_with_its_type():
    def empty(quick):
        raise KeyError()

    [result] = selfcheck.run([("empty", empty)])
    assert (result.status, result.detail) == ("fail", "KeyError")


def test_a_check_that_answers_nonsense_fails():
    [unknown, shapeless] = selfcheck.run([("unknown", answers("great")), ("shapeless", lambda quick: None)])
    assert (unknown.status, unknown.detail) == ("fail", "unknown status 'great'")
    assert shapeless.status == "fail"


def test_a_check_past_its_timeout_fails_and_the_next_one_still_runs():
    release = threading.Event()
    try:
        hung, after = selfcheck.run([("hung", lambda quick: release.wait(5) and ("ok", "")), ("after", answers("ok"))], timeout=0.05)
    finally:
        release.set()
    assert (hung.status, hung.detail) == ("fail", "no answer in 0.05 s")
    assert after.status == "ok"


def test_once_the_budget_is_spent_the_checks_left_are_skipped():
    ticks = iter([0, 0, 0, 125, 125])  # began; the first check: left, start, end; the second: left, none
    results = selfcheck.run([("first", answers("ok")), ("second", answers("ok"))], budget=120, clock=lambda: next(ticks))
    assert [result.status for result in results] == ["ok", "skip"]
    assert results[1].detail == "no time left of the 120 s budget"


def test_summary_counts_each_status():
    results = selfcheck.run([("a", answers("ok")), ("b", answers("ok")), ("c", answers("warn")), ("d", answers("skip"))])
    assert selfcheck.summary(results, 41.6) == "2 ok, 1 warn, 0 fail, 1 skipped in 42s"


def test_the_checks_are_the_thirteen_named_ones_in_order():
    assert [name for name, _ in selfcheck.CHECKS] == [
        "mpv", "YouTube search", "YouTube stream", "SoundCloud", "Podcasts", "Lyrics", "Album art",
        "Last.fm", "ListenBrainz", "Spotify", "Server", "Stream", "Data dir",
    ]


# --- mpv ----------------------------------------------------------------


def test_mpv_missing_fails_with_the_doctor_line():
    assert selfcheck.check_mpv(False, doctor=lambda: (False, "mpv not found on PATH")) == ("fail", "mpv not found on PATH")


def test_mpv_plays_the_silence_and_says_how_fast():
    assert selfcheck.check_mpv(False, doctor=lambda: (True, "mpv v0.39.0"), play=lambda: 0.4) == (
        "ok", "mpv v0.39.0; played 1 s of silence, started in 0.4 s",
    )


def test_mpv_with_quick_only_checks_the_version():
    def play():
        raise AssertionError("no playback with --quick")

    assert selfcheck.check_mpv(True, doctor=lambda: (True, "mpv v0.39.0"), play=play) == (
        "ok", "mpv v0.39.0; playback skipped with --quick",
    )


def test_the_silent_wav_is_one_second_of_zeros():
    with wave.open(io.BytesIO(selfcheck.silent_wav())) as wav:
        frames = wav.readframes(wav.getnframes())
        assert wav.getnframes() == wav.getframerate()
    assert set(frames) == {0}


class FakeMpv:
    """Stands in for MpvClient: on play_current it reports a restart (or an error) through on_state, as mpv would."""

    instances = []

    def __init__(self, video, on_state, levels, prefetch, resume, started_in=0.3, error=None, silent=False):
        self.on_state, self.started_in, self.error, self.silent = on_state, None, None, silent
        self.outcome = (started_in, error)
        self.queue = []
        self.quit_called = False
        FakeMpv.instances.append(self)

    def add(self, video):
        self.queue.append(video)

    def play_current(self):
        with open(self.queue[0].url, "rb") as file:
            assert file.read(4) == b"RIFF"
        if self.silent:
            return
        self.started_in, self.error = self.outcome
        self.on_state({"started_in": self.started_in, "error": self.error})

    def quit(self):
        self.quit_called = True


def mpv_factory(**outcome):
    return lambda *args, **kwargs: FakeMpv(*args, **kwargs, **outcome)


def test_play_silence_answers_started_in_and_quits_mpv():
    assert selfcheck.play_silence(mpv_factory(started_in=0.3)) == 0.3
    assert FakeMpv.instances[-1].quit_called
    assert FakeMpv.instances[-1].queue[0].source == "file"


def test_play_silence_fails_with_mpv_s_error():
    with pytest.raises(selfcheck.CheckFailed, match="^Could not play selfcheck silence: no audio device$"):
        selfcheck.play_silence(mpv_factory(started_in=None, error="Could not play selfcheck silence: no audio device"))
    assert FakeMpv.instances[-1].quit_called


def test_play_silence_fails_when_mpv_never_starts_it():
    with pytest.raises(selfcheck.CheckFailed, match="did not start a 1 s WAV within 0.01 s"):
        selfcheck.play_silence(mpv_factory(silent=True), wait=0.01)
    assert FakeMpv.instances[-1].quit_called


# --- lookups ------------------------------------------------------------


def searching(*videos):
    calls = []

    def search(query, limit, source="youtube"):
        calls.append((query, limit, source))
        return list(videos)

    search.calls = calls
    return search


def test_youtube_search_names_the_first_result():
    search = searching(SONG)
    assert selfcheck.check_youtube_search(False, search=search) == ("ok", "found Song")
    assert search.calls == [("Queen Bohemian Rhapsody", 1, "youtube")]


def test_youtube_search_with_no_result_fails():
    with pytest.raises(selfcheck.CheckFailed, match="YouTube found nothing"):
        selfcheck.check_youtube_search(False, search=searching())


def test_youtube_stream_resolves_the_first_result():
    resolved = []
    status = selfcheck.check_youtube_stream(False, search=searching(SONG), resolve=lambda url: resolved.append(url) or "https://media")
    assert status == ("ok", "resolved Song")
    assert resolved == [SONG.url]


def test_youtube_stream_without_a_media_url_fails():
    with pytest.raises(selfcheck.CheckFailed, match="yt-dlp found no stream"):
        selfcheck.check_youtube_stream(False, search=searching(SONG), resolve=lambda url: None)


def test_youtube_stream_is_skipped_with_quick():
    def search(*args, **kwargs):
        raise AssertionError("no lookup with --quick")

    assert selfcheck.check_youtube_stream(True, search=search) == ("skip", "skipped with --quick")


def test_soundcloud_searches_soundcloud():
    search = searching(SONG)
    assert selfcheck.check_soundcloud(False, search=search) == ("ok", "found Song")
    assert search.calls[0][2] == "soundcloud"


def test_soundcloud_with_no_result_fails():
    with pytest.raises(selfcheck.CheckFailed, match="SoundCloud found nothing"):
        selfcheck.check_soundcloud(False, search=searching())


def test_podcasts_names_the_show_and_its_newest_episode():
    episode = Video(id="e1", title="Morning edition", uploader="NPR", duration=600, source="podcast")
    assert selfcheck.check_podcasts(False, latest=lambda query, limit: (SHOW, [episode])) == ("ok", "News Now: Morning edition")


def test_podcasts_without_a_show_or_an_episode_fail():
    with pytest.raises(selfcheck.CheckFailed, match="iTunes found no show"):
        selfcheck.check_podcasts(False, latest=lambda query, limit: (None, []))
    with pytest.raises(selfcheck.CheckFailed, match="the feed of News Now lists no episodes"):
        selfcheck.check_podcasts(False, latest=lambda query, limit: (SHOW, []))


def test_lyrics_wants_synced_lines():
    synced = Lyrics([(0.0, "Is this the real life?"), (4.0, "Is this just fantasy?")], None, "https://lrclib.net/api/get/1")
    assert selfcheck.check_lyrics(False, request=lambda *args: synced) == ("ok", "2 synced lines for Queen – Bohemian Rhapsody")


def test_lyrics_with_plain_lines_only_warns_and_none_fails():
    plain = Lyrics(None, "Is this the real life?", "https://lrclib.net/api/get/1")
    assert selfcheck.check_lyrics(False, request=lambda *args: plain)[0] == "warn"
    with pytest.raises(selfcheck.CheckFailed, match='No lyrics found for "Queen – Bohemian Rhapsody"'):
        selfcheck.check_lyrics(False, request=lambda *args: None)


def test_art_without_the_extra_is_skipped_with_the_hint():
    assert selfcheck.check_art(False, doctor=lambda: (None, "album art not installed: hint"), search=searching()) == (
        "skip", "album art not installed: hint",
    )


def test_art_fetches_the_first_result_s_thumbnail():
    fetched = []
    status = selfcheck.check_art(False, doctor=lambda: (True, "album art"), search=searching(SONG), fetch=lambda url: fetched.append(url) or b"jpeg")
    assert status == ("ok", "album art; a 4 byte thumbnail")
    assert fetched == [SONG.thumbnail]


def test_art_fails_without_a_thumbnail_or_its_bytes():
    bare = Video(id="abc", title="Song", uploader="Band", duration=200)
    with pytest.raises(selfcheck.CheckFailed, match="Song has no thumbnail"):
        selfcheck.check_art(False, doctor=lambda: (True, "album art"), search=searching(bare))
    with pytest.raises(selfcheck.CheckFailed, match="cannot fetch the thumbnail"):
        selfcheck.check_art(False, doctor=lambda: (True, "album art"), search=searching(SONG), fetch=lambda url: None)


def youtube_checks(search, doctor=lambda: (True, "album art")):
    """The three checks that use the YouTube search, all given the one fake search."""
    return [
        ("YouTube search", functools.partial(selfcheck.check_youtube_search, search=search)),
        ("YouTube stream", functools.partial(selfcheck.check_youtube_stream, search=search, resolve=lambda url: "https://media")),
        ("Album art", functools.partial(selfcheck.check_art, doctor=doctor, search=search, fetch=lambda url: b"jpeg")),
    ]


def test_the_youtube_search_runs_once_per_run():
    search = searching(SONG)
    results = selfcheck.run(youtube_checks(search))
    assert [result.status for result in results] == ["ok", "ok", "ok"]
    assert len(search.calls) == 1
    selfcheck.run(youtube_checks(search))
    assert len(search.calls) == 2


def test_an_empty_search_fails_once_and_its_dependents_skip():
    search = searching()
    results = selfcheck.run(youtube_checks(search))
    assert [(result.status, result.detail) for result in results] == [
        ("fail", "YouTube found nothing for 'Queen Bohemian Rhapsody'"),
        ("skip", "needs the YouTube search check to pass"),
        ("skip", "needs the YouTube search check to pass"),
    ]
    assert len(search.calls) == 1
    assert selfcheck.summary(results, 0).startswith("0 ok, 0 warn, 1 fail, 2 skipped")


def test_a_search_that_raises_fails_once_with_its_error_and_its_dependents_skip():
    calls = []

    def search(query, limit, source="youtube"):
        calls.append(query)
        raise OSError("no route to host")

    results = selfcheck.run(youtube_checks(search))
    assert [(result.status, result.detail) for result in results] == [
        ("fail", "no route to host"),
        ("skip", "needs the YouTube search check to pass"),
        ("skip", "needs the YouTube search check to pass"),
    ]
    assert calls == ["Queen Bohemian Rhapsody"]
    assert selfcheck.summary(results, 0).startswith("0 ok, 0 warn, 1 fail, 2 skipped")


def test_a_search_past_its_timeout_is_not_repeated_and_its_dependents_skip():
    release = threading.Event()
    calls = []

    def search(query, limit, source="youtube"):
        calls.append(query)
        release.wait(5)
        return [SONG]

    try:
        results = selfcheck.run(youtube_checks(search), timeout=0.05)
    finally:
        release.set()
    assert [(result.status, result.detail) for result in results] == [
        ("fail", "no answer in 0.05 s"),
        ("skip", "needs the YouTube search check to pass"),
        ("skip", "needs the YouTube search check to pass"),
    ]
    assert calls == ["Queen Bohemian Rhapsody"]


def test_art_reports_a_missing_extra_before_looking_at_the_search():
    results = selfcheck.run(youtube_checks(searching(), doctor=lambda: (None, "album art not installed: hint")))
    assert (results[2].status, results[2].detail) == ("skip", "album art not installed: hint")


# --- logins -------------------------------------------------------------


class FakeLastFm(scrobble.LastFm):
    def __init__(self, logged_in=True, reply=None):
        super().__init__()
        self.is_logged_in = logged_in
        self.reply = reply or {"user": {"name": "ana"}}
        self.calls = []

    def logged_in(self):
        return self.is_logged_in

    def load_session(self):
        return {"api_key": "k", "secret": "s", "session_key": "sk", "user": "ana"}

    def call(self, method, params, api_key, secret, session_key=None):
        self.calls.append((method, session_key))
        return self.reply


def test_lastfm_logged_out_is_skipped():
    assert selfcheck.check_lastfm(False, target=FakeLastFm(logged_in=False)) == ("skip", "not logged in (ttyplayer lastfm login)")


def test_lastfm_asks_who_the_session_belongs_to():
    target = FakeLastFm()
    assert selfcheck.check_lastfm(False, target=target) == ("ok", "logged in as ana")
    assert target.calls == [("user.getInfo", "sk")]


def test_lastfm_with_an_odd_reply_fails():
    with pytest.raises(scrobble.ScrobbleError):
        selfcheck.check_lastfm(False, target=FakeLastFm(reply={"user": {}}))


class FakeListenBrainz(scrobble.ListenBrainz):
    def __init__(self, logged_in=True, reply=None):
        super().__init__()
        self.is_logged_in = logged_in
        self.reply = reply
        self.calls = []

    def logged_in(self):
        return self.is_logged_in

    def load_session(self):
        return {"token": "t0k", "user_name": "ana"}

    def request(self, endpoint, token, body=None):
        self.calls.append((endpoint, token))
        return self.reply


def test_listenbrainz_logged_out_is_skipped():
    assert selfcheck.check_listenbrainz(False, target=FakeListenBrainz(logged_in=False))[0] == "skip"


def test_listenbrainz_validates_the_stored_token():
    target = FakeListenBrainz(reply={"valid": True, "user_name": "ana"})
    assert selfcheck.check_listenbrainz(False, target=target) == ("ok", "logged in as ana")
    assert target.calls == [("validate-token", "t0k")]


def test_listenbrainz_with_a_token_it_no_longer_takes_warns():
    status, detail = selfcheck.check_listenbrainz(False, target=FakeListenBrainz(reply={"valid": False}))
    assert status == "warn"
    assert detail.endswith("run ttyplayer listenbrainz login again")


def spotify_check(tmp_path, client_id="id", token=True, user_name=lambda refresh: "Ana"):
    path = tmp_path / "spotify.json"
    if token:
        path.write_text("{}")
    load = lambda: settings.Settings(spotify_client_id=client_id)  # noqa: E731
    return selfcheck.check_spotify(False, load=load, token_path=lambda: path, user_name=user_name)


def test_spotify_without_a_client_id_or_a_login_is_skipped(tmp_path):
    assert spotify_check(tmp_path, client_id="")[0] == "skip"
    (tmp_path / "spotify.json").unlink()
    assert spotify_check(tmp_path, token=False) == ("skip", "not logged in (ttyplayer spotify login)")


def test_spotify_asks_who_is_logged_in(tmp_path):
    assert spotify_check(tmp_path) == ("ok", "logged in as Ana")


def test_spotify_refusing_the_login_warns_to_log_in_again(tmp_path):
    def refused(refresh):
        raise spotify.SpotifyError("Spotify answered HTTP 401: invalid token")

    assert spotify_check(tmp_path, user_name=refused) == ("warn", f"Spotify answered HTTP 401: invalid token. {spotify.LOGIN_AGAIN}")

    def says_it(refresh):
        raise spotify.SpotifyError(f"Cannot refresh the Spotify login (bad). {spotify.LOGIN_AGAIN}")

    assert spotify_check(tmp_path, user_name=says_it) == ("warn", f"Cannot refresh the Spotify login (bad). {spotify.LOGIN_AGAIN}")


def test_spotify_reports_a_run_out_token_without_refreshing_or_writing_it(monkeypatch, tmp_path):
    path = tmp_path / "spotify.json"
    path.write_text(json.dumps({"client_id": "id", "access_token": "old", "refresh_token": "r", "expires_at": 0}))
    before = path.read_bytes()
    monkeypatch.setattr(spotify, "token_path", lambda: path)
    monkeypatch.setattr(spotify, "request_tokens", lambda *args, **kwargs: pytest.fail("selfcheck refreshed the token"))
    load = lambda: settings.Settings(spotify_client_id="id")  # noqa: E731
    status, detail = selfcheck.check_spotify(False, load=load, token_path=lambda: path)
    assert (status, detail) == ("warn", f"The stored Spotify access token has run out. {spotify.LOGIN_AGAIN}")
    assert path.read_bytes() == before


# --- this machine -------------------------------------------------------


def test_server_serves_status_to_its_token_and_401_to_a_wrong_one():
    status, detail = selfcheck.check_server(False)
    assert status == "ok", detail
    assert detail.endswith("a wrong token got 401")


def test_server_that_lets_a_wrong_token_in_fails():
    class Opened:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def close(self):
            pass

    with pytest.raises(selfcheck.CheckFailed, match="a wrong token got in"):
        selfcheck.check_server(False, opener=lambda request, timeout: Opened())


@pytest.mark.parametrize("prefix", ["", "x"])
def test_server_that_answers_a_wrong_token_otherwise_fails(monkeypatch, prefix):
    token_urlsafe = selfcheck.secrets.token_urlsafe
    monkeypatch.setattr(selfcheck.secrets, "token_urlsafe", lambda nbytes: prefix + token_urlsafe(nbytes))
    good = []

    def opener(request, timeout):
        header = request.headers["Authorization"]
        if not good:
            good.append(header)
        if header != good[0]:
            raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, None)
        return selfcheck.direct_open(request, timeout=timeout)

    with pytest.raises(selfcheck.CheckFailed, match="a wrong token got HTTP 500, not 401"):
        selfcheck.check_server(False, opener=opener)


def test_ffmpeg_follows_the_doctor_row():
    assert selfcheck.check_ffmpeg(False, doctor=lambda: (None, "ffmpeg not found")) == ("skip", "ffmpeg not found")
    assert selfcheck.check_ffmpeg(False, doctor=lambda: (True, "ffmpeg version 7")) == ("ok", "ffmpeg version 7")
    assert selfcheck.check_ffmpeg(False, doctor=lambda: (False, "ffmpeg broke")) == ("fail", "ffmpeg broke")


def test_data_dir_counts_what_each_file_holds(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    history.record(SONG)
    history.record(SONG)
    favorites.add(SONG)
    playlists.create("mix")
    playlists.add("mix", [SONG, SONG])
    positions.set("abc", 61)
    status, detail = selfcheck.check_data(False)
    assert status == "ok"
    assert detail == f"history 2, favorites 1, playlists 1 (2 videos), positions 1, subscriptions 0 in {tmp_path / 'ttyplayer'}"


def test_data_dir_with_corrupt_lines_fails_naming_the_files(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    history.record(SONG)
    playlists.create("mix")
    playlists.add("mix", [SONG])
    with history.history_path().open("a") as file:
        file.write("{broken\n")
    with playlists.playlist_path("mix").open("a") as file:
        file.write("not json\n[1]\n")
    status, detail = selfcheck.check_data(False)
    assert status == "fail"
    assert detail == f"history.jsonl: 1 corrupt line; mix.jsonl: 2 corrupt lines in {tmp_path / 'ttyplayer'}"


def test_data_dir_with_an_unreadable_subscriptions_file_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    subscriptions.subscriptions_path().parent.mkdir(parents=True)
    subscriptions.subscriptions_path().write_text("{broken")
    status, detail = selfcheck.check_data(False)
    assert status == "fail"
    assert detail.startswith("subscriptions.json: Expecting property name enclosed in double quotes")
    assert capsys.readouterr().err == ""


def test_data_dir_that_is_empty_counts_zeros(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert selfcheck.check_data(False)[1].startswith("history 0, favorites 0, playlists 0 (0 videos), positions 0, subscriptions 0")


# --- the command --------------------------------------------------------


FAKE_CHECKS = [("mpv", answers("ok", "mpv v0.39.0")), ("Spotify", answers("skip", "not logged in")), ("Lyrics", answers("warn", "plain only"))]


def test_selfcheck_prints_a_row_per_check_and_the_summary(monkeypatch):
    monkeypatch.setattr(selfcheck, "CHECKS", FAKE_CHECKS)
    result = runner.invoke(app, ["selfcheck"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("✓ mpv      mpv v0.39.0  (")
    assert lines[1].startswith("– Spotify  not logged in  (")
    assert lines[2].startswith("⚠ Lyrics   plain only  (")
    assert lines[3] == "1 ok, 1 warn, 0 fail, 1 skipped in 0s"


def test_selfcheck_exits_1_when_a_check_fails(monkeypatch):
    monkeypatch.setattr(selfcheck, "CHECKS", [*FAKE_CHECKS, ("YouTube search", raises)])
    result = runner.invoke(app, ["selfcheck"])
    assert result.exit_code == 1
    assert "✗ YouTube search  no route to host  (" in result.output
    assert result.output.splitlines()[-1].startswith("1 ok, 1 warn, 1 fail, 1 skipped in ")


def test_selfcheck_json_prints_the_results(monkeypatch):
    monkeypatch.setattr(selfcheck, "CHECKS", [*FAKE_CHECKS, ("YouTube search", raises)])
    result = runner.invoke(app, ["selfcheck", "--json"])
    assert result.exit_code == 1
    rows = json.loads(result.output)
    assert [(row["name"], row["status"], row["detail"]) for row in rows] == [
        ("mpv", "ok", "mpv v0.39.0"), ("Spotify", "skip", "not logged in"), ("Lyrics", "warn", "plain only"),
        ("YouTube search", "fail", "no route to host"),
    ]
    assert all(isinstance(row["seconds"], float) for row in rows)


def test_selfcheck_quick_reaches_the_checks(monkeypatch):
    seen = []
    monkeypatch.setattr(selfcheck, "CHECKS", [("mpv", lambda quick: seen.append(quick) or ("ok", "v"))])
    assert runner.invoke(app, ["selfcheck", "--quick"]).exit_code == 0
    assert seen == [True]


def test_doctor_and_selfcheck_share_the_marks():
    assert selfcheck.STATUSES == ("ok", "warn", "fail", "skip")
    assert {cli.mark_line(status, "x")[0] for status in selfcheck.STATUSES} == {"✓", "⚠", "✗", "–"}


def test_playback_waits_three_seconds_by_default():
    assert selfcheck.PLAYBACK_WAIT == 3


def test_art_downloads_the_thumbnail_without_touching_the_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    class Response(io.BytesIO):
        pass

    data = art.download(SONG.thumbnail, opener=lambda url, timeout: Response(b"jpeg"))
    assert data == b"jpeg"
    assert not art.cache_dir().exists()
    assert inspect.signature(selfcheck.check_art).parameters["fetch"].default is art.download
