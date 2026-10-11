import io
import json
import os
import stat
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from conftest import posix_only
from ttyplayer import scrobble, settings
from ttyplayer.models import Video
from ttyplayer.scrobble import ScrobbleError

SONG = Video(id="s", title="Daft Punk - Around the World (Official Video)", uploader="DaftPunkVEVO", duration=429)
EPISODE = Video(id="e", title="Episode 12: Tea", uploader="The Tea Show", duration=3600, source="podcast", link="https://x/e.mp3")
SESSION = {"api_key": "KEY", "secret": "SECRET", "session_key": "SK", "user": "ann"}


@pytest.fixture(autouse=True)
def homes(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path


class FakeLastfm:
    """urlopen for scrobble: every request is a POST to API_URL; answers by method from queued replies
    (a dict is JSON, a (code, body) tuple an HTTP error, an Exception is raised)."""

    def __init__(self, monkeypatch):
        self.replies = {}
        self.requests = []
        monkeypatch.setattr(scrobble.urllib.request, "urlopen", self)

    def on(self, method, *replies):
        self.replies.setdefault(method, []).extend(replies)

    def __call__(self, request, timeout=None):
        assert request.full_url == scrobble.API_URL and request.get_method() == "POST"
        form = dict(urllib.parse.parse_qsl(request.data.decode()))
        self.requests.append(form)
        queued = self.replies.get(form["method"])
        if not queued:
            raise AssertionError(f"unexpected call {form['method']}")
        reply = queued.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, tuple):
            code, body = reply
            raise urllib.error.HTTPError(request.full_url, code, "Forbidden", {}, io.BytesIO(body.encode()))
        return io.BytesIO(json.dumps(reply).encode())

    def methods(self):
        return [form["method"] for form in self.requests]


@pytest.fixture
def fake(monkeypatch):
    return FakeLastfm(monkeypatch)


def logged_in():
    scrobble.save_session(SESSION)


def signed_correctly(form, secret="SECRET"):
    unsigned = {name: value for name, value in form.items() if name != "api_sig"}
    return form["api_sig"] == scrobble.sign(unsigned, secret)


IGNORED = {3: "Timestamp failed filter", 5: "Daily scrobble limit exceeded"}


def answered(*codes):
    """A track.scrobble reply in Last.fm's JSON shape: one result per scrobble sent (an object, not a list, for one),
    each with its ignoredMessage code, 0 when taken."""
    results = [
        {"artist": {"corrected": "0", "#text": "A"}, "track": {"corrected": "0", "#text": "T"}, "timestamp": "1",
         "ignoredMessage": {"code": str(code), "#text": IGNORED.get(code, "")}}
        for code in codes
    ]
    taken = codes.count(0)
    return {"scrobbles": {"scrobble": results[0] if len(results) == 1 else results, "@attr": {"accepted": taken, "ignored": len(codes) - taken}}}


ACCEPTED = answered(0)
UNREACHABLE = urllib.error.URLError("Network is unreachable")


# --- signing --------------------------------------------------------------


def test_sign_follows_the_api_docs_example():
    # last.fm/api/authspec §8: api_key, method, token sorted by name, each name then value, the secret appended, md5.
    params = {"token": "yyyyyy", "method": "auth.getSession", "api_key": "xxxxxxxxxx"}
    assert scrobble.sign(params, "ilovecher") == "b87d61da3cda91a8b6746c4aef55d6f8"


def test_sign_leaves_out_format_and_callback_and_hashes_utf8():
    params = {"artist": "Sigur Rós", "track": "Hoppípolla", "method": "track.scrobble"}
    assert scrobble.sign({**params, "format": "json", "callback": "x"}, "s") == scrobble.sign(params, "s")
    assert scrobble.sign(params, "s") != scrobble.sign({**params, "artist": "Sigur Ros"}, "s")


def test_call_posts_a_signed_json_request(fake):
    fake.on("track.updateNowPlaying", {"nowplaying": {}})
    scrobble.call("track.updateNowPlaying", {"artist": "A", "track": "T"}, "KEY", "SECRET", "SK")
    [form] = fake.requests
    assert form == form | {"artist": "A", "track": "T", "api_key": "KEY", "sk": "SK", "format": "json"}
    assert signed_correctly(form)


@pytest.mark.parametrize(
    "reply, message",
    [
        ((403, "<html>blocked</html>"), "Last.fm answered error 403: Forbidden"),
        ((400, '{"error": 6, "message": "Invalid parameters"}'), "Last.fm answered error 6: Invalid parameters"),
        ({"error": 11, "message": "Service Offline"}, "Last.fm answered error 11: Service Offline"),
        ({"error": 9, "message": "Invalid session key"}, "Last.fm did not accept the login (Invalid session key); run ttyplayer lastfm login again"),
        (UNREACHABLE, "Cannot reach Last.fm: Network is unreachable"),
        (None, "Last.fm sent an unexpected reply to track.scrobble"),
        (["scrobbles"], "Last.fm sent an unexpected reply to track.scrobble"),
    ],
)
def test_call_turns_every_failure_into_one_line(fake, reply, message):
    fake.on("track.scrobble", reply)
    with pytest.raises(ScrobbleError) as raised:
        scrobble.call("track.scrobble", {}, "KEY", "SECRET", "SK")
    assert str(raised.value) == message
    assert "\n" not in str(raised.value)


# --- login, logout, status -----------------------------------------------------------


def test_login_gets_a_token_sends_the_user_to_allow_it_then_stores_the_session(fake):
    fake.on("auth.getToken", {"token": "TOK"})
    fake.on("auth.getSession", {"session": {"name": "ann", "key": "SK", "subscriber": 0}})
    opened, echoed, waited = [], [], []
    assert scrobble.login("KEY", "SECRET", opened.append, echoed.append, waited.append) == "ann"
    assert opened == ["https://www.last.fm/api/auth/?api_key=KEY&token=TOK"]
    assert opened[0] in echoed[0]
    assert len(waited) == 1  # the session is asked for only after the user pressed Enter
    assert fake.methods() == ["auth.getToken", "auth.getSession"]
    assert fake.requests[1]["token"] == "TOK" and "sk" not in fake.requests[1]
    assert all(signed_correctly(form) for form in fake.requests)
    assert scrobble.load_session() == SESSION


def test_login_not_allowed_in_the_browser_stores_nothing(fake):
    fake.on("auth.getToken", {"token": "TOK"})
    fake.on("auth.getSession", (403, '{"error": 14, "message": "This token has not been authorized"}'))
    with pytest.raises(ScrobbleError, match="run ttyplayer lastfm login again"):
        scrobble.login("KEY", "SECRET", lambda url: None, lambda text: None, lambda prompt: "")
    assert not scrobble.session_path().exists()


@pytest.mark.parametrize(
    "token_reply, session_reply, message",
    [
        ({}, None, "no token"),
        ({"token": "TOK"}, {"session": {"name": "ann"}}, "no session.key"),
        ({"token": "TOK"}, {"session": "ann"}, "no session.name"),
    ],
)
def test_login_with_an_unexpected_reply_stores_nothing(fake, token_reply, session_reply, message):
    fake.on("auth.getToken", token_reply)
    fake.on("auth.getSession", session_reply)
    with pytest.raises(ScrobbleError, match=f"Last.fm sent an unexpected reply \\({message}\\)"):
        scrobble.login("KEY", "SECRET", lambda url: None, lambda text: None, lambda prompt: "")
    assert not scrobble.session_path().exists()


def test_the_session_lives_next_to_the_settings(homes):
    assert scrobble.session_path() == settings.settings_path().parent / "lastfm.json"


@posix_only
def test_the_session_file_is_readable_only_by_the_user():
    path = scrobble.session_path()
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    os.chmod(path, 0o644)
    logged_in()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@posix_only
def test_the_secret_is_never_on_disk_readable_by_others(monkeypatch):
    path = scrobble.session_path()
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    os.chmod(path, 0o644)
    replaced = []
    real_replace = os.replace

    def replace(source, target):
        # the moment the new file takes the old one's place: its mode, and what the old file held until then
        replaced.append((stat.S_IMODE(os.stat(source).st_mode), "SECRET" in Path(source).read_text(), path.read_text()))
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", replace)
    logged_in()
    assert replaced == [(0o600, True, "{}")]
    assert [entry.name for entry in path.parent.iterdir()] == ["lastfm.json"]


@pytest.mark.parametrize("text", ["{}", "null", "[]", '{"user": "ann"}', '{"api_key": "KEY", "secret": "S", "session_key": 7, "user": "ann"}'])
def test_a_session_file_that_is_no_login_says_to_log_in_again(fake, text):
    path = scrobble.session_path()
    path.parent.mkdir(parents=True)
    path.write_text(text)
    message = f"{path} is not a Last.fm login; run ttyplayer lastfm login again"
    with pytest.raises(ScrobbleError) as raised:
        scrobble.now_playing(SONG)
    assert str(raised.value) == message
    assert scrobble.user() is None and fake.requests == []


def test_logout_removes_the_session_and_status_follows():
    assert scrobble.user() is None and scrobble.logout() is False
    logged_in()
    assert scrobble.user() == "ann"
    assert scrobble.logout() is True
    assert scrobble.user() is None and not scrobble.session_path().exists()


def test_without_a_login_a_call_says_to_log_in():
    with pytest.raises(ScrobbleError, match="Not logged in to Last.fm: run ttyplayer lastfm login"):
        scrobble.now_playing(SONG)


def test_active_needs_the_setting_and_a_login():
    assert not scrobble.active(settings.Settings())
    logged_in()
    assert scrobble.active(settings.Settings())
    assert not scrobble.active(settings.Settings(scrobble=False))


# --- what is sent --------------------------------------------------------


def test_track_is_the_lyrics_guess_with_the_duration():
    assert scrobble.track(SONG) == {"artist": "Daft Punk", "track": "Around the World", "duration": 429}


def test_track_of_a_podcast_has_the_show_as_its_artist_and_no_album():
    assert scrobble.track(EPISODE) == {"artist": "The Tea Show", "track": "Episode 12: Tea", "duration": 3600}


def test_track_without_a_duration_sends_none():
    assert scrobble.track(Video(id="x", title="Song", uploader="Band - Topic", duration=None)) == {"artist": "Band", "track": "Song"}


def test_wanted_takes_podcasts_only_with_scrobble_podcasts():
    assert scrobble.wanted(SONG, False) and scrobble.wanted(SONG, True)
    assert scrobble.wanted(EPISODE, True) and not scrobble.wanted(EPISODE, False)


@pytest.mark.parametrize(
    "duration, seconds",
    [(None, 240), (20, None), (30, None), (31, 15.5), (180, 90), (480, 240), (3600, 240)],
)
def test_threshold_is_half_the_track_or_four_minutes_whichever_comes_first(duration, seconds):
    assert scrobble.threshold(duration) == seconds


def test_now_playing_sends_the_track_signed_with_the_session(fake):
    logged_in()
    fake.on("track.updateNowPlaying", {"nowplaying": {}})
    scrobble.now_playing(SONG)
    [form] = fake.requests
    assert (form["artist"], form["track"], form["duration"], form["sk"]) == ("Daft Punk", "Around the World", "429", "SK")
    assert "album" not in form and signed_correctly(form)


def test_scrobble_sends_the_start_time(fake):
    logged_in()
    fake.on("track.scrobble", ACCEPTED)
    scrobble.scrobble(SONG, 1_700_000_000.7)
    [form] = fake.requests
    assert form == form | {"artist[0]": "Daft Punk", "track[0]": "Around the World", "timestamp[0]": "1700000000", "duration[0]": "429"}
    assert signed_correctly(form)
    assert scrobble.queued() == 0


# --- the offline queue ----------------------------------------------------


def test_a_failed_scrobble_is_queued_with_artist_track_and_time_only(fake):
    logged_in()
    fake.on("track.scrobble", UNREACHABLE)
    with pytest.raises(ScrobbleError, match="Cannot reach Last.fm"):
        scrobble.scrobble(SONG, 1000)
    lines = scrobble.queue_path().read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"artist": "Daft Punk", "track": "Around the World", "timestamp": 1000}]


def test_the_next_scrobble_sends_the_queued_ones_first(fake):
    logged_in()
    fake.on("track.scrobble", UNREACHABLE, answered(0, 0))
    with pytest.raises(ScrobbleError):
        scrobble.scrobble(SONG, 1000)
    scrobble.scrobble(EPISODE, 2000)
    form = fake.requests[-1]
    assert (form["timestamp[0]"], form["timestamp[1]"], form["artist[1]"]) == ("1000", "2000", "The Tea Show")
    assert not scrobble.queue_path().exists()


def test_now_playing_sends_the_queued_scrobbles_once_last_fm_answers(fake):
    logged_in()
    scrobble.save_queue([{"artist": "A", "track": "T", "timestamp": 5}])
    fake.on("track.updateNowPlaying", {"nowplaying": {}})
    fake.on("track.scrobble", ACCEPTED)
    scrobble.now_playing(SONG)
    assert fake.methods() == ["track.updateNowPlaying", "track.scrobble"]
    assert scrobble.queued() == 0


def test_the_queue_is_sent_fifty_per_call_and_a_failure_keeps_the_rest(fake):
    logged_in()
    scrobble.save_queue([{"artist": "A", "track": f"T{n}", "timestamp": n} for n in range(120)])
    fake.on("track.scrobble", answered(*[0] * 50), answered(*[0] * 50), UNREACHABLE)
    with pytest.raises(ScrobbleError):
        scrobble.send_queued()
    assert [sum(name.startswith("timestamp[") for name in form) for form in fake.requests] == [50, 50, 20]
    assert fake.requests[1]["track[0]"] == "T50"
    remaining = [json.loads(line)["track"] for line in scrobble.queue_path().read_text(encoding="utf-8").splitlines()]
    assert remaining == [f"T{n}" for n in range(100, 120)]


def test_last_fm_ignoring_scrobbles_keeps_those_over_the_daily_limit_and_drops_the_rest(fake):
    logged_in()
    scrobble.save_queue([{"artist": "A", "track": f"T{n}", "timestamp": n} for n in range(3)])
    fake.on("track.scrobble", answered(0, 5, 3))
    with pytest.raises(ScrobbleError) as raised:
        scrobble.send_queued()
    assert str(raised.value) == "Last.fm ignored 2 scrobble(s): Daily scrobble limit exceeded; 1 kept to send again"
    assert [entry["track"] for entry in scrobble.queued_entries()] == ["T1"]


def test_one_scrobble_over_the_daily_limit_stays_queued(fake):
    logged_in()
    fake.on("track.scrobble", answered(5))  # accepted 0, ignored 1: one result object, not a list
    with pytest.raises(ScrobbleError, match="Last.fm ignored 1 scrobble"):
        scrobble.scrobble(SONG, 1000)
    assert scrobble.queued() == 1


@pytest.mark.parametrize(
    "reply",
    [
        {"scrobbles": {"@attr": {"accepted": 2, "ignored": 0}}},  # no result per scrobble
        answered(0),  # one result for two scrobbles
        {"scrobbles": {"scrobble": [{"ignoredMessage": {"code": "x"}}] * 2}},
        {"scrobbles": "yes"},
        # one scrobble answered, the other without a valid ignoredMessage code: neither is taken as accepted
        *({"scrobbles": {"scrobble": [answered(0)["scrobbles"]["scrobble"], result]}} for result in (
            {},
            {"ignoredMessage": None},
            {"ignoredMessage": "0"},
            {"ignoredMessage": {}},
            {"ignoredMessage": {"code": None}},
            {"ignoredMessage": {"code": ""}},
            # a code that is not an explicit whole number is not read as one: no float truncation, no bool coercion
            {"ignoredMessage": {"code": 0.5}},
            {"ignoredMessage": {"code": 0.0}},
            {"ignoredMessage": {"code": False}},
            {"ignoredMessage": {"code": True}},
            {"ignoredMessage": {"code": "0.5"}},
            {"ignoredMessage": {"code": " 0"}},
            {"ignoredMessage": {"code": "+0"}},
            {"ignoredMessage": {"code": "-0"}},
            {"ignoredMessage": {"code": "\u0660"}},  # Arabic-Indic zero: int() reads it, Last.fm never sends it
        )),
    ],
)
def test_a_reply_that_does_not_answer_each_scrobble_keeps_them_queued(fake, reply):
    logged_in()
    scrobble.save_queue([{"artist": "A", "track": "T", "timestamp": 5}])
    fake.on("track.scrobble", reply)
    with pytest.raises(ScrobbleError, match="^Last.fm sent an unexpected reply to track.scrobble$"):
        scrobble.scrobble(SONG, 1000)
    assert [entry["timestamp"] for entry in scrobble.queued_entries()] == [5, 1000]


def test_a_single_result_without_its_ignored_message_keeps_the_scrobble_queued(fake):
    logged_in()
    fake.on("track.scrobble", {"scrobbles": {"scrobble": {}, "@attr": {"accepted": 0, "ignored": 1}}})
    with pytest.raises(ScrobbleError, match="^Last.fm sent an unexpected reply to track.scrobble$"):
        scrobble.scrobble(SONG, 1000)
    assert scrobble.queued() == 1


def test_a_code_sent_as_a_json_number_is_read_like_its_string(fake):
    logged_in()
    reply = answered(0, 5)
    reply["scrobbles"]["scrobble"][0]["ignoredMessage"]["code"] = 0
    reply["scrobbles"]["scrobble"][1]["ignoredMessage"]["code"] = 5
    fake.on("track.scrobble", reply)
    scrobble.save_queue([{"artist": "A", "track": "T", "timestamp": 5}])
    with pytest.raises(ScrobbleError, match="Last.fm ignored 1 scrobble"):
        scrobble.scrobble(SONG, 1000)
    assert [entry["timestamp"] for entry in scrobble.queued_entries()] == [1000]


def test_a_code_too_long_to_read_keeps_the_queue(fake):
    logged_in()
    scrobble.save_queue([{"artist": "A", "track": "T", "timestamp": 5}])
    reply = answered(0)
    reply["scrobbles"]["scrobble"]["ignoredMessage"]["code"] = "0" * 4301
    fake.on("track.scrobble", reply)
    with pytest.raises(ScrobbleError, match="^Last.fm sent an unexpected reply to track.scrobble$"):
        scrobble.send_queued()
    assert scrobble.queued() == 1


def test_a_queue_line_without_artist_track_and_time_is_skipped():
    scrobble.queue_path().parent.mkdir(parents=True)
    scrobble.queue_path().write_text('{"timestamp": 1}\n{"artist": "A", "track": "T", "timestamp": 2}\n', encoding="utf-8")
    assert scrobble.queued_entries() == [{"artist": "A", "track": "T", "timestamp": 2}]


def test_the_queue_keeps_the_newest_500():
    scrobble.save_queue([{"artist": "A", "track": "T", "timestamp": n} for n in range(510)])
    assert scrobble.queued() == 500
    assert json.loads(scrobble.queue_path().read_text(encoding="utf-8").splitlines()[0])["timestamp"] == 10


def test_a_scrobble_while_logged_out_is_queued_for_after_the_login(fake):
    with pytest.raises(ScrobbleError, match="Not logged in"):
        scrobble.scrobble(SONG, 1000)
    assert scrobble.queued() == 1 and fake.requests == []


def test_nothing_queued_sends_nothing(fake):
    logged_in()
    scrobble.send_queued()
    assert fake.requests == []
