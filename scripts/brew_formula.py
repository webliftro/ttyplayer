#!/usr/bin/env python3
"""Print the Homebrew formula for a released ttyplayer, built from its sdist on PyPI.

Usage: python3 -I scripts/brew_formula.py <version> > Formula/ttyplayer.rb

Every runtime dependency becomes a `resource` pointing at its sdist (Homebrew builds them in the
formula's virtualenv, native ones included). The dependency list is the released wheel's
Requires-Dist, followed recursively through PyPI, with each version pinned to uv.lock when it
records one and the latest release otherwise. Markers are evaluated for Homebrew's Python on macOS.
Standard library only.
"""

import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = "ttyplayer"
DESC = "Modern YouTube player for the terminal"
PYTHON = "3.13"
LOCK = Path(__file__).resolve().parent.parent / "uv.lock"

# The marker environment of python@3.13 on a Mac; `extra` is filled in per requested extra.
MARKER_ENV = {
    "os_name": "posix",
    "sys_platform": "darwin",
    "platform_system": "Darwin",
    "platform_machine": "arm64",
    "platform_python_implementation": "CPython",
    "implementation_name": "cpython",
    "python_version": PYTHON,
    "python_full_version": f"{PYTHON}.0",
    "implementation_version": f"{PYTHON}.0",
}
VERSION_VARIABLES = {"python_version", "python_full_version", "implementation_version"}

REQUIREMENT = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?[^;]*(?:;(.*))?$")
MARKER_TOKEN = re.compile(r"""\s*(\(|\)|not\s+in\b|in\b|and\b|or\b|===|==|!=|<=|>=|~=|<|>|'[^']*'|"[^"]*"|[A-Za-z_.]+)""")

TEMPLATE = """\
class Ttyplayer < Formula
  include Language::Python::Virtualenv

  desc "{desc}"
  homepage "{homepage}"
  url "{url}"
  sha256 "{sha256}"
  license "{license}"

  depends_on "mpv"
  depends_on "python@{python}"
  # Only `ttyplayer serve --stream` uses ffmpeg, and doctor reports it as optional.
  depends_on "ffmpeg" => :optional
{resources}
  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match version.to_s, shell_output("#{{bin}}/ttyplayer version")
  end
end
"""

RESOURCE = """
  resource "{name}" do
    url "{url}"
    sha256 "{sha256}"
  end
"""


class FormulaError(Exception):
    pass


def normalize(name):
    """PEP 503: one spelling per project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def fetch_json(name, version=None):
    """The PyPI JSON of one release (the latest without a version)."""
    url = f"https://pypi.org/pypi/{name}/{version}/json" if version else f"https://pypi.org/pypi/{name}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        label = f"{name} {version}" if version else name
        raise FormulaError(f"{label} is not on PyPI ({error.code})") from None


def lock_pins(path=LOCK):
    """{name: version} for every package uv.lock records, or {} without a lock."""
    if not path.exists():
        return {}
    with path.open("rb") as file:
        return {normalize(package["name"]): package["version"] for package in tomllib.load(file).get("package", [])}


def version_key(text):
    return tuple(int(part) for part in re.findall(r"\d+", text))


def compare(variable, op, left, right):
    if op == "in":
        return left in right
    if op == "not in":
        return left not in right
    if variable == "extra":
        left, right = normalize(left), normalize(right)
    if variable in VERSION_VARIABLES and op not in ("==", "!=", "==="):
        left, right = version_key(left), version_key(right)
        op = ">=" if op == "~=" else op
    elif right.endswith(".*") and op in ("==", "!="):
        return (left + ".").startswith(right[:-1]) == (op == "==")
    return {
        "==": left == right,
        "===": left == right,
        "!=": left != right,
        "<": left < right,
        "<=": left <= right,
        ">": left > right,
        ">=": left >= right,
    }[op]


def evaluate(marker, extra=""):
    """Whether a PEP 508 marker holds on the formula's platform with the given extra."""
    tokens = MARKER_TOKEN.findall(marker)
    environment = {**MARKER_ENV, "extra": extra}
    position = 0

    def take():
        nonlocal position
        position += 1
        return re.sub(r"\s+", " ", tokens[position - 1])

    def value():
        token = take()
        if token[0] in "'\"":
            return None, token[1:-1]
        return token, environment.get(token, "")

    def atom():
        if tokens[position] == "(":
            take()
            result = either()
            take()
            return result
        variable, left = value()
        op = take()
        other, right = value()
        return compare(variable or other, op, left, right)

    def both():
        result = atom()
        while position < len(tokens) and tokens[position] == "and":
            take()
            result = atom() and result
        return result

    def either():
        result = both()
        while position < len(tokens) and tokens[position] == "or":
            take()
            result = both() or result
        return result

    return either()


def requirements(requires_dist, extras=frozenset()):
    """(name, extras) of each requirement that applies on the formula's platform."""
    for line in requires_dist or []:
        name, wanted, marker = REQUIREMENT.match(line).groups()
        if not marker or any(evaluate(marker, extra) for extra in {"", *extras}):
            yield normalize(name), frozenset(normalize(extra) for extra in (wanted or "").split(",") if extra.strip())


def sdist(release):
    """(name, url, sha256) of a release's sdist; FormulaError when PyPI has none."""
    info = release["info"]
    for file in release["urls"]:
        if file["packagetype"] == "sdist":
            return normalize(info["name"]), file["url"], file["digests"]["sha256"]
    raise FormulaError(f"{info['name']} {info['version']} has no sdist on PyPI; Homebrew needs one to build it")


def resources(requires_dist, fetch, pins):
    """The sorted sdists of every runtime dependency, followed recursively."""
    extras, releases = {}, {}
    todo = list(requirements(requires_dist))
    while todo:
        name, wanted = todo.pop()
        if name == PROJECT or (name in extras and wanted <= extras[name]):
            continue
        extras[name] = extras.get(name, frozenset()) | wanted
        if name not in releases:
            releases[name] = fetch(name, pins.get(name))
        todo.extend(requirements(releases[name]["info"]["requires_dist"], extras[name]))
    return sorted(sdist(release) for release in releases.values())


def formula(version, fetch=fetch_json, pins=None):
    """The formula text for ttyplayer <version>."""
    release = fetch(PROJECT, version)
    info = release["info"]
    _, url, sha256 = sdist(release)
    deps = resources(info["requires_dist"], fetch, lock_pins() if pins is None else pins)
    return TEMPLATE.format(
        desc=DESC,
        homepage=info["project_urls"]["Homepage"],
        url=url,
        sha256=sha256,
        license=info["license_expression"],
        python=PYTHON,
        resources="".join(RESOURCE.format(name=name, url=url, sha256=sha256) for name, url, sha256 in deps),
    )


def main(argv):
    if len(argv) != 1:
        print("usage: brew_formula.py <version>", file=sys.stderr)
        return 2
    try:
        sys.stdout.write(formula(argv[0]))
    except FormulaError as error:
        print(f"brew_formula: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
