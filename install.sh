#!/bin/sh
# Install ttyplayer on macOS or Linux: uv, mpv and ttyplayer itself, then `ttyplayer doctor`.
#
#   ./install.sh                     from a checkout (installs that checkout)
#   curl -LsSf https://raw.githubusercontent.com/webliftro/ttyplayer/main/install.sh | sh
#
# TTYPLAYER_INSTALL_DRY_RUN=1 prints the commands without running them.
# TTYPLAYER_INSTALL_OS=macos|debian|fedora|arch|suse|alpine skips platform detection (for tests).
#
# Every command is printed with a leading "+ " before it runs. Only uv's official installer is
# piped into a shell, and it runs as you; sudo is used only for the system package manager.
# The mpv commands below are the ones `ttyplayer doctor` prints (MPV_INSTALL in cli.py).
set -eu

UV_INSTALL_URL="https://astral.sh/uv/install.sh"
HOMEBREW_INSTALL="/bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
REPO_GIT_URL="git+https://github.com/webliftro/ttyplayer"
EXTRAS="[art]"  # the optional parts every install gets: album art in the TUI
MPV_INSTALL_DOCS="https://mpv.io/installation/"

DRY_RUN="${TTYPLAYER_INSTALL_DRY_RUN:-0}"
ORIGINAL_PATH="$PATH"

say() {
    printf '==> %s\n' "$1"
}

die() {
    printf 'error: %s\n' "$1" >&2
    exit 1
}

# Print a command, then run it unless this is a dry run.
run() {
    printf '+ %s\n' "$1"
    if [ "$DRY_RUN" != 1 ]; then
        eval "$1"
    fi
}

has() {
    command -v "$1" >/dev/null 2>&1
}

detect_os() {
    case "${TTYPLAYER_INSTALL_OS:-}" in
        "") ;;
        macos | debian | fedora | arch | suse | alpine) echo "$TTYPLAYER_INSTALL_OS"; return ;;
        *) die "unknown TTYPLAYER_INSTALL_OS '$TTYPLAYER_INSTALL_OS' (use macos, debian, fedora, arch, suse or alpine)" ;;
    esac
    case "$(uname -s)" in
        Darwin) echo macos ;;
        Linux)
            if has apt-get; then echo debian
            elif has dnf; then echo fedora
            elif has pacman; then echo arch
            elif has zypper; then echo suse
            elif has apk; then echo alpine
            else echo linux
            fi
            ;;
        MINGW* | MSYS* | CYGWIN*) die "on Windows run install.ps1 in PowerShell instead" ;;
        *) die "unsupported system $(uname -s); install uv, mpv and ttyplayer by hand (see the README)" ;;
    esac
}

mpv_command() {
    case "$1" in
        macos) echo "brew install mpv" ;;
        debian) echo "sudo apt-get install -y mpv" ;;
        fedora) echo "sudo dnf install -y mpv" ;;
        arch) echo "sudo pacman -S --noconfirm mpv" ;;
        suse) echo "sudo zypper install -y mpv" ;;
        alpine) echo "sudo apk add mpv" ;;
        *) die "no supported package manager found; install mpv from $MPV_INSTALL_DOCS and run this again" ;;
    esac
}

install_uv() {
    if has uv; then
        say "uv found: $(command -v uv)"
        return
    fi
    say "installing uv as you (no sudo)"
    run "curl -LsSf $UV_INSTALL_URL | sh"
    PATH="$HOME/.local/bin:$PATH"
    if [ "$DRY_RUN" != 1 ] && ! has uv; then
        die "uv did not install; is curl available? See https://docs.astral.sh/uv/getting-started/installation/"
    fi
}

install_mpv() {
    os="$1"
    if has mpv; then
        say "mpv found: $(command -v mpv)"
        return
    fi
    mpv_cmd="$(mpv_command "$os")"
    if [ "$os" = macos ] && ! has brew; then
        printf 'mpv needs Homebrew (https://brew.sh). Install Homebrew with:\n\n  %s\n\nthen run this script again.\n' "$HOMEBREW_INSTALL" >&2
        exit 1
    fi
    say "installing mpv"
    run "$mpv_cmd"
}

in_checkout() {
    [ -f pyproject.toml ] && grep -q '^name = "ttyplayer"' pyproject.toml
}

install_ttyplayer() {
    if in_checkout; then
        run "uv tool install --force '.$EXTRAS'"
    elif ! run "uv tool install --force 'ttyplayer$EXTRAS'"; then
        say "no ttyplayer release on PyPI yet; installing from GitHub"
        run "uv tool install --force 'ttyplayer$EXTRAS @ $REPO_GIT_URL'"
    fi
    if [ "$DRY_RUN" != 1 ]; then
        PATH="$(uv tool dir --bin):$PATH"
    fi
}

check_new_shell_path() {
    if (PATH="$ORIGINAL_PATH"; has ttyplayer); then
        return
    fi
    say "ttyplayer is not on the PATH of new shells yet. Fix it with:"
    printf '\n  uv tool update-shell\n\nthen open a new terminal.\n'
}

main() {
    os="$(detect_os)"
    install_uv
    install_mpv "$os"
    install_ttyplayer
    run "ttyplayer doctor"
    check_new_shell_path
}

main
