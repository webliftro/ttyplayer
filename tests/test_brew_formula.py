import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "brew_formula.py"
GOLDEN = Path(__file__).parent / "fixtures" / "brew_formula.rb"

spec = importlib.util.spec_from_file_location("brew_formula", SCRIPT)
brew_formula = importlib.util.module_from_spec(spec)
spec.loader.exec_module(brew_formula)


def release(name, version, requires=(), sdist=True):
    files = [{"packagetype": "bdist_wheel", "url": f"https://files.example/{name}-{version}-py3-none-any.whl", "digests": {"sha256": "w" * 64}}]
    if sdist:
        files.append({"packagetype": "sdist", "url": f"https://files.example/{name}-{version}.tar.gz", "digests": {"sha256": f"{name}-{version}-sha"}})
    return {
        "info": {
            "name": name,
            "version": version,
            "requires_dist": list(requires) or None,
            "license_expression": "MIT",
            "project_urls": {"Homepage": "https://github.com/webliftro/ttyplayer"},
        },
        "urls": files,
    }


# A small PyPI: {name: {version: release}}; the last version listed is the latest.
PYPI = {
    "ttyplayer": {"1.0.0": release("ttyplayer", "1.0.0", [
        "aiohttp>=3.10", "textual>=8", "pytest; extra == 'dev'", "textual-image>=0.14.1; extra == 'art'", "pillow>=12.3; extra == 'art'"
    ])},
    "textual-image": {"0.14.1": release("textual-image", "0.14.1", ["pillow", "rich"])},
    "rich": {"14.0.0": release("rich", "14.0.0")},
    "aiohttp": {"3.14.4": release("aiohttp", "3.14.4", ["Yarl<2,>=1.17", 'brotli; platform_python_implementation == "CPython" and extra == "speedups"'])},
    "yarl": {"1.0.0": release("yarl", "1.0.0"), "1.22.0": release("yarl", "1.22.0", ["idna>=2.0"])},
    "idna": {"3.20": release("idna", "3.20")},
    "textual": {"8.2.0": release("textual", "8.2.0", ["markdown-it-py[linkify]>=2.1", 'colorama; sys_platform == "win32"', "typing_extensions>=4; python_version < '3.11'"])},
    "markdown-it-py": {"4.2.0": release("markdown-it-py", "4.2.0", ["mdurl~=0.1", 'linkify-it-py<3,>=1; extra == "linkify"'])},
    "mdurl": {"0.1.2": release("mdurl", "0.1.2")},
    "linkify-it-py": {"2.2.0": release("linkify-it-py", "2.2.0")},
}


class FakePypi:
    def __init__(self, pypi=PYPI):
        self.pypi = pypi
        self.calls = []

    def __call__(self, name, version=None):
        self.calls.append((name, version))
        versions = self.pypi[name]
        return versions[version or list(versions)[-1]]


def test_formula_matches_the_golden_file():
    assert brew_formula.formula("1.0.0", fetch=FakePypi(), pins={}) == GOLDEN.read_text()


def test_resources_follow_requirements_extras_and_markers():
    names = [name for name, _, _ in brew_formula.resources(PYPI["ttyplayer"]["1.0.0"]["info"]["requires_dist"], FakePypi(), {})]
    # Sorted; dev extras, Windows-only and old-Python requirements dropped; markdown-it-py[linkify] adds linkify-it-py.
    assert names == ["aiohttp", "idna", "linkify-it-py", "markdown-it-py", "mdurl", "textual", "yarl"]


def test_the_formula_takes_the_art_extra_with_homebrews_pillow():
    deps, formulas = brew_formula.dependencies(PYPI["ttyplayer"]["1.0.0"]["info"]["requires_dist"], FakePypi(), {}, {"art"})
    assert [name for name, _, _ in deps] == [
        "aiohttp", "idna", "linkify-it-py", "markdown-it-py", "mdurl", "rich", "textual", "textual-image", "yarl"
    ]
    assert formulas == ["pillow"]  # BREW_FORMULAS: a depends_on, never a resource
    assert brew_formula.EXTRAS == {"art"} and brew_formula.BREW_FORMULAS == {"pillow": "pillow"}


def test_resources_pass_the_sdist_url_and_sha_through():
    deps = brew_formula.resources(["idna"], FakePypi(), {})
    assert deps == [("idna", "https://files.example/idna-3.20.tar.gz", "idna-3.20-sha")]


def test_lock_pins_win_over_the_latest_release():
    fetch = FakePypi()
    deps = brew_formula.resources(["yarl"], fetch, {"yarl": "1.0.0"})
    assert deps == [("yarl", "https://files.example/yarl-1.0.0.tar.gz", "yarl-1.0.0-sha")]
    assert fetch.calls == [("yarl", "1.0.0")]


def test_unpinned_dependencies_take_the_latest_release():
    fetch = FakePypi()
    brew_formula.resources(["yarl"], fetch, {})
    assert fetch.calls == [("yarl", None), ("idna", None)]


def test_a_dependency_without_an_sdist_fails_with_one_line():
    pypi = {**PYPI, "idna": {"3.20": release("idna", "3.20", sdist=False)}}
    with pytest.raises(brew_formula.FormulaError) as error:
        brew_formula.resources(["idna"], FakePypi(pypi), {})
    assert str(error.value) == "idna 3.20 has no sdist on PyPI; Homebrew needs one to build it"


def test_lock_pins_read_uv_lock(tmp_path):
    lock = tmp_path / "uv.lock"
    lock.write_text('[[package]]\nname = "Typing_Extensions"\nversion = "4.15.0"\n')
    assert brew_formula.lock_pins(lock) == {"typing-extensions": "4.15.0"}
    assert brew_formula.lock_pins(tmp_path / "missing.lock") == {}


@pytest.mark.parametrize(
    "marker, extra, expected",
    [
        ('sys_platform == "darwin"', "", True),
        ("sys_platform == 'win32'", "", False),
        ('python_version < "3.11"', "", False),
        ('python_version >= "3.9"', "", True),
        ('python_full_version >= "3.13.0" and platform_system != "Windows"', "", True),
        ('extra == "socks"', "", False),
        ('extra == "Socks"', "socks", True),
        ('(sys_platform == "win32" or os_name == "posix") and extra == "x"', "x", True),
        ('sys_platform == "win32" or (os_name == "nt" and python_version > "3")', "", False),
        ('"arm" in platform_machine', "", True),
        ('platform_machine not in "x86_64 AMD64"', "", True),
    ],
)
def test_evaluate_markers_for_homebrew_python_on_macos(marker, extra, expected):
    assert brew_formula.evaluate(marker, extra) is expected


def test_script_runs_isolated_and_needs_a_version():
    result = subprocess.run([sys.executable, "-I", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 2
    assert result.stderr == "usage: brew_formula.py <version>\n"
