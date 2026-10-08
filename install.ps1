# Install ttyplayer on Windows: uv, mpv and ttyplayer itself, then `ttyplayer doctor`.
#
#   .\install.ps1                    from a checkout (installs that checkout)
#   irm https://raw.githubusercontent.com/webliftro/ttyplayer/main/install.ps1 | iex
#
# -DryRun prints the commands without running them.
#
# Every command is printed with a leading "+ " before it runs. Only uv's official installer is
# piped into a shell, and it runs as you. Runs under Windows PowerShell 5.1 and pwsh 7.
# The mpv command below is the one `ttyplayer doctor` prints (MPV_INSTALL in cli.py).
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$UvInstallCommand = 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
# `winget search mpv` lists shinchiro.mpv (the mpv.io Windows builds) and mpv.net (a different
# player); there is no mpv.mpv package.
$MpvInstallCommand = "winget install -e --id shinchiro.mpv"
$AppInstallerUrl = "https://apps.microsoft.com/detail/9NBLGGH4NNS1"
$RepoGitUrl = "git+https://github.com/webliftro/ttyplayer"
$UvBin = Join-Path $HOME ".local\bin"
# shinchiro.mpv's installer puts mpv.exe in {autopf}\MPV Player and leaves PATH alone.
$MpvDirName = "MPV Player"

function Write-Info([string]$Text) {
    Write-Host "==> $Text"
}

function Test-Command([string]$Name) {
    [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# Print a command, then run it unless this is a dry run. Returns whether it succeeded.
function Invoke-Step([string]$Command) {
    Write-Host "+ $Command"
    if ($DryRun) {
        return $true
    }
    $global:LASTEXITCODE = 0
    Invoke-Expression $Command | Out-Host
    return $LASTEXITCODE -eq 0
}

function Invoke-RequiredStep([string]$Command) {
    if (-not (Invoke-Step $Command)) {
        throw "'$Command' failed with exit code $LASTEXITCODE"
    }
}

# The PATH a new shell starts with.
function Get-SavedPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    "$machine;$user"
}

# uv's installer and Add-MpvToPath change the saved PATH; pick that up without opening a new shell.
function Update-SessionPath {
    $env:Path = "$UvBin;$(Get-SavedPath)"
}

function Install-Uv {
    if (Test-Command "uv") {
        Write-Info "uv found: $((Get-Command uv).Source)"
        return
    }
    Write-Info "installing uv (as you, no administrator rights)"
    Invoke-RequiredStep $UvInstallCommand
    if (-not $DryRun) {
        Update-SessionPath
        if (-not (Test-Command "uv")) {
            throw "uv did not install; see https://docs.astral.sh/uv/getting-started/installation/"
        }
    }
}

function Install-Mpv {
    if (Test-Command "mpv") {
        Write-Info "mpv found: $((Get-Command mpv).Source)"
        return
    }
    if (-not (Test-Command "winget")) {
        Write-Host "mpv needs winget. Install App Installer from the Microsoft Store:"
        Write-Host ""
        Write-Host "  $AppInstallerUrl"
        Write-Host ""
        Write-Host "then run this script again."
        throw "winget not found"
    }
    Write-Info "installing mpv"
    Invoke-RequiredStep $MpvInstallCommand
    if (-not $DryRun) {
        Add-MpvToPath
    }
}

# Put the folder winget installed mpv.exe into on the user PATH (new shells) and this session's.
function Add-MpvToPath {
    $roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, "$env:LOCALAPPDATA\Programs") | Where-Object { $_ }
    $dirs = $roots | ForEach-Object { Join-Path $_ $MpvDirName }
    $mpvDir = $dirs | Where-Object { Test-Path (Join-Path $_ "mpv.exe") } | Select-Object -First 1
    if (-not $mpvDir) {
        throw "winget installed mpv but mpv.exe is not in any of: $($dirs -join ', ')"
    }
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (($userPath -split ";") -notcontains $mpvDir) {
        Write-Info "adding $mpvDir to your PATH"
        [Environment]::SetEnvironmentVariable("Path", (@($userPath, $mpvDir) | Where-Object { $_ }) -join ";", "User")
    }
    Update-SessionPath
}

function Test-Checkout {
    (Test-Path "pyproject.toml") -and (Select-String -Path "pyproject.toml" -Pattern '^name = "ttyplayer"' -Quiet)
}

function Install-Ttyplayer {
    if (Test-Checkout) {
        Invoke-RequiredStep "uv tool install --force ."
    }
    elseif (-not (Invoke-Step "uv tool install --force ttyplayer")) {
        Write-Info "no ttyplayer release on PyPI yet; installing from GitHub"
        Invoke-RequiredStep "uv tool install --force $RepoGitUrl"
    }
    if (-not $DryRun) {
        $env:Path = "$(uv tool dir --bin);$env:Path"
    }
}

function Test-NewShellPath {
    $saved = $env:Path
    $env:Path = Get-SavedPath
    $found = Test-Command "ttyplayer"
    $env:Path = $saved
    if (-not $found) {
        Write-Info "ttyplayer is not on the PATH of new shells yet. Fix it with:"
        Write-Host ""
        Write-Host "  uv tool update-shell"
        Write-Host ""
        Write-Host "then open a new terminal."
    }
}

Install-Uv
Install-Mpv
Install-Ttyplayer
Invoke-RequiredStep "ttyplayer doctor"
Test-NewShellPath
