import os
import re
import subprocess
from pathlib import Path

import pytest

from conftest import posix_only

ROOT = Path(__file__).resolve().parent.parent
RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def job(name):
    """The text of one job in release.yml, up to the next job (yaml is not a dependency)."""
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", RELEASE, re.M | re.S)
    assert match, f"no {name} job"
    return match.group(1)


def step_script(name):
    """The `run: |` block of a homebrew step, dedented."""
    match = re.search(rf"- name: {name}\n\s+run: \|\n((?:          .*\n|\n)+)", job("homebrew"))
    assert match, f"no run block for {name}"
    return "".join(line[10:] + "\n" for line in match.group(1).splitlines())


def run_step(script, tmp_path, fakes):
    """Run a step script with bash -e (GitHub's default shell) and fake commands first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command, body in fakes.items():
        (bin_dir / command).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / command).chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GITHUB_REF_NAME": "v1.2.3", "RUNNER_TEMP": str(tmp_path)}
    return subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True)


def test_wait_polls_the_json_api_for_twenty_minutes():
    wait = step_script("Wait for the release on PyPI")
    assert 'url="https://pypi.org/pypi/ttyplayer/$version/json"' in wait
    assert 'version="${GITHUB_REF_NAME#v}"' in wait
    assert re.search(r'curl -fsS .*"\$url"', wait)
    assert "deadline=$((start + 1200))" in wait and "attempt + 20" in wait
    assert "pip download" not in job("homebrew")


def clock_fakes(tmp_path, curl):
    """Fake date/sleep/curl sharing a clock file; curl logs "<start> <args>" then runs `curl`."""
    clock = tmp_path / "clock"
    clock.write_text("0\n")
    return {
        "date": f"cat {clock}",
        "sleep": f'echo $(($(cat {clock}) + $1)) > {clock}',
        "curl": f'echo "$(cat {clock}) $*" >> {tmp_path}/calls\n{curl}',
    }


@posix_only
def test_wait_proceeds_on_the_first_200(tmp_path):
    result = run_step(step_script("Wait for the release on PyPI"), tmp_path, clock_fakes(tmp_path, f"[ $(wc -l < {tmp_path}/calls) -ge 5 ]"))
    assert result.returncode == 0
    calls = (tmp_path / "calls").read_text().splitlines()
    assert [int(call.split()[0]) for call in calls] == [0, 20, 40, 60, 80]
    assert calls[0].endswith("https://pypi.org/pypi/ttyplayer/1.2.3/json")
    assert result.stdout.splitlines() == ["Waiting for ttyplayer 1.2.3 on PyPI (1 min)"]


@posix_only
@pytest.mark.parametrize("request_takes", ["no time", "its whole --max-time"])
def test_wait_gives_up_after_twenty_minutes_with_one_line_per_minute(tmp_path, request_takes):
    clock = tmp_path / "clock"
    advance = f'echo $(($(cat {clock}) + $3)) > {clock}\n' if request_takes != "no time" else ""
    result = run_step(step_script("Wait for the release on PyPI"), tmp_path, clock_fakes(
        tmp_path, f"{advance}echo 'curl: (22) The requested URL returned error: 404' >&2; exit 22"))
    assert result.returncode == 1
    assert int(clock.read_text()) == 1200
    calls = (tmp_path / "calls").read_text().splitlines()
    assert [int(call.split()[0]) for call in calls] == list(range(0, 1200, 20))
    assert all(call.split()[1:4] == ["-fsS", "--max-time", "10"] for call in calls)
    assert result.stdout.splitlines() == [f"Waiting for ttyplayer 1.2.3 on PyPI ({minute} min)" for minute in range(1, 20)]
    assert result.stderr == "ttyplayer 1.2.3 did not appear on PyPI within 20 minutes\n"


@posix_only
def test_generator_failure_fails_the_step_without_a_formula(tmp_path):
    script = step_script("Generate the formula")
    assert 'python3 -I scripts/brew_formula.py "${GITHUB_REF_NAME#v}"' in script
    result = run_step(script, tmp_path, {"python3": "echo 'class Ttyplayer < Formula'; exit 1"})
    assert result.returncode != 0
    assert not (tmp_path / "Formula" / "ttyplayer.rb").exists()


@posix_only
def test_generator_success_writes_the_formula(tmp_path):
    result = run_step(step_script("Generate the formula"), tmp_path, {"python3": "echo 'class Ttyplayer < Formula'"})
    assert result.returncode == 0
    assert (tmp_path / "Formula" / "ttyplayer.rb").read_text() == "class Ttyplayer < Formula\n"
