import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ttyplayer import cli

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "install.sh"
INSTALL_PS1 = ROOT / "install.ps1"
UV_INSTALL = "curl -LsSf https://astral.sh/uv/install.sh | sh"

LINUX_MPV = dict(zip(["debian", "fedora", "arch", "suse", "alpine"], cli.MPV_INSTALL["linux"], strict=True))
MPV_COMMANDS = {"macos": cli.MPV_INSTALL["darwin"][0], **LINUX_MPV}

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="install.sh is for macOS and Linux")


def fake_commands(directory, *names, output=""):
    directory.mkdir(exist_ok=True)
    for name in names:
        path = directory / name
        path.write_text(f"#!/bin/sh\necho {output}\n" if output else "#!/bin/sh\n")
        path.chmod(0o755)
    return directory


def run_install(tmp_path, cwd, fakes=(), os_name=None, uname=None):
    """Dry-run install.sh with PATH holding only the given fake commands and grep."""
    bin_dir = fake_commands(tmp_path / "bin", *fakes)
    (bin_dir / "grep").symlink_to(shutil.which("grep"))
    if uname:
        fake_commands(bin_dir, "uname", output=uname)
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path), "TTYPLAYER_INSTALL_DRY_RUN": "1"}
    if os_name:
        env["TTYPLAYER_INSTALL_OS"] = os_name
    return subprocess.run([shutil.which("sh"), str(INSTALL_SH)], cwd=cwd, env=env, capture_output=True, text=True)


def commands(result):
    return [line.removeprefix("+ ") for line in result.stdout.splitlines() if line.startswith("+ ")]


@posix_only
def test_install_sh_is_valid_posix_sh():
    subprocess.run([shutil.which("sh"), "-n", str(INSTALL_SH)], check=True)


@posix_only
@pytest.mark.parametrize("os_name", MPV_COMMANDS)
def test_install_sh_installs_everything_missing(tmp_path, os_name):
    result = run_install(tmp_path, ROOT, fakes=["brew"], os_name=os_name)
    assert result.returncode == 0, result.stderr
    assert commands(result) == [UV_INSTALL, MPV_COMMANDS[os_name], "uv tool install --force '.[art]'", "ttyplayer doctor"]


@posix_only
@pytest.mark.parametrize("os_name", MPV_COMMANDS)
def test_install_sh_skips_uv_and_mpv_when_present(tmp_path, os_name):
    result = run_install(tmp_path, ROOT, fakes=["uv", "mpv"], os_name=os_name)
    assert result.returncode == 0, result.stderr
    assert commands(result) == ["uv tool install --force '.[art]'", "ttyplayer doctor"]


@posix_only
def test_install_sh_outside_a_checkout_installs_from_pypi(tmp_path):
    result = run_install(tmp_path, tmp_path, fakes=["uv", "mpv"], os_name="debian")
    assert commands(result) == ["uv tool install --force 'ttyplayer[art]'", "ttyplayer doctor"]


@posix_only
def test_install_sh_prints_how_to_fix_the_path_of_new_shells(tmp_path):
    result = run_install(tmp_path, ROOT, fakes=["uv", "mpv"], os_name="debian")
    assert "uv tool update-shell" in result.stdout


@posix_only
def test_install_sh_on_macos_without_homebrew_prints_its_installer_and_fails(tmp_path):
    result = run_install(tmp_path, ROOT, fakes=["uv"], os_name="macos")
    assert result.returncode == 1
    assert "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh" in result.stderr
    assert commands(result) == []


@posix_only
@pytest.mark.parametrize("manager, os_name", [("apt-get", "debian"), ("dnf", "fedora"), ("apk", "alpine")])
def test_install_sh_detects_the_linux_package_manager(tmp_path, manager, os_name):
    result = run_install(tmp_path, ROOT, fakes=["uv", manager], uname="Linux")
    assert result.returncode == 0, result.stderr
    assert MPV_COMMANDS[os_name] in commands(result)


@posix_only
def test_install_sh_detects_macos(tmp_path):
    result = run_install(tmp_path, ROOT, fakes=["uv", "brew"], uname="Darwin")
    assert MPV_COMMANDS["macos"] in commands(result)


@posix_only
def test_install_sh_without_a_package_manager_points_at_mpv_io(tmp_path):
    result = run_install(tmp_path, ROOT, fakes=["uv"], uname="Linux")
    assert result.returncode == 1
    assert "https://mpv.io/installation/" in result.stderr


@posix_only
def test_install_sh_rejects_an_unknown_forced_os(tmp_path):
    result = run_install(tmp_path, ROOT, os_name="plan9")
    assert result.returncode == 1
    assert "unknown TTYPLAYER_INSTALL_OS 'plan9'" in result.stderr


def test_install_sh_uses_sudo_only_for_the_package_manager():
    code = [line for line in INSTALL_SH.read_text().splitlines() if not line.lstrip().startswith("#")]
    sudo_lines = [line for line in code if "sudo " in line]
    assert len(sudo_lines) == len(cli.MPV_INSTALL["linux"])
    for line, command in zip(sudo_lines, cli.MPV_INSTALL["linux"], strict=True):
        assert f'echo "{command}"' in line


@pytest.mark.parametrize("script, variable", [(INSTALL_SH, "$EXTRAS"), (INSTALL_PS1, "$Extras")])
def test_install_scripts_name_the_art_extra_in_one_place(script, variable):
    text = script.read_text()
    assert text.count("[art]") == 1
    # a checkout, PyPI, then GitHub: each install line takes the extra from that one place
    for target in (f"'.{variable}'", f"'ttyplayer{variable}'", f"'ttyplayer{variable} @ "):
        assert f"uv tool install --force {target}" in text


@posix_only
def test_install_sh_falls_back_to_github_with_the_extra_as_one_argument(tmp_path):
    # Not a dry run: a fake uv records its arguments one per line and has no PyPI release to give.
    bin_dir = fake_commands(tmp_path / "bin", "mpv", "ttyplayer")
    (bin_dir / "grep").symlink_to(shutil.which("grep"))
    log = tmp_path / "uv.log"
    (bin_dir / "uv").write_text(
        f'#!/bin/sh\nfor arg; do echo "$arg"; done >> {log}\necho -- >> {log}\n'
        f'[ "$4" = "ttyplayer[art]" ] && exit 1\necho {bin_dir}\n'
    )
    (bin_dir / "uv").chmod(0o755)
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path), "TTYPLAYER_INSTALL_OS": "debian"}
    result = subprocess.run([shutil.which("sh"), str(INSTALL_SH)], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text().split("--\n")
    assert calls[0].splitlines() == ["tool", "install", "--force", "ttyplayer[art]"]
    assert calls[1].splitlines() == ["tool", "install", "--force", "ttyplayer[art] @ git+https://github.com/webliftro/ttyplayer"]


def test_install_ps1_installs_the_doctor_winget_command():
    assert f'$MpvInstallCommand = "{cli.MPV_INSTALL["win32"][0]}"' in INSTALL_PS1.read_text()



@pytest.fixture
def user_path():
    """The user PATH in the registry, as a [get, set] pair; restored after the test."""
    import winreg

    def key(access=winreg.KEY_READ):
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, access)

    def get():
        with key() as k:
            return winreg.QueryValueEx(k, "Path")[0]

    def set_(value):
        with key(winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, value)

    with key() as k:
        try:
            original = winreg.QueryValueEx(k, "Path")
        except FileNotFoundError:
            original = None
    yield get, set_
    with key(winreg.KEY_SET_VALUE) as k:
        if original is None:
            winreg.DeleteValue(k, "Path")
        else:
            winreg.SetValueEx(k, "Path", 0, original[1], original[0])


@pytest.mark.skipif(sys.platform != "win32", reason="install.ps1 changes the Windows user PATH")
def test_install_ps1_puts_the_winget_mpv_on_path(tmp_path, user_path):
    # winget succeeds but, like shinchiro.mpv, leaves the saved PATH alone.
    get_user_path, set_user_path = user_path
    mpv_dir = tmp_path / "appdata" / "Programs" / "MPV Player"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv.cmd").write_text("@exit /b 0\n")
    (bin_dir / "winget.cmd").write_text(f'@mkdir "{mpv_dir}"\n@type nul > "{mpv_dir}\\mpv.exe"\n')
    (bin_dir / "ttyplayer.cmd").write_text("@where mpv\n")
    # The script rebuilds $env:Path from the saved PATH after installing mpv; keep the fakes on it.
    set_user_path(f"{bin_dir};{get_user_path()}")
    system32 = Path(os.environ["SystemRoot"]) / "System32"
    env = {
        **os.environ,
        "PATH": f"{bin_dir};{system32}",
        "LOCALAPPDATA": str(tmp_path / "appdata"),
        "ProgramFiles": str(tmp_path / "pf"),
        "ProgramFiles(x86)": str(tmp_path / "pf86"),
    }
    shell = shutil.which("pwsh") or shutil.which("powershell")
    result = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(INSTALL_PS1)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{mpv_dir}\\mpv.exe" in result.stdout
    assert str(mpv_dir) in get_user_path().split(";")
