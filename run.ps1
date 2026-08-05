# Windows counterpart to run.sh - same steps, same defaults.
# Launch via run.bat (which bypasses the PowerShell execution policy that
# blocks unsigned .ps1 files by default), or directly with:
#   powershell -ExecutionPolicy Bypass -File run.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Prefer the py launcher (installed with python.org builds) and fall back to
# whatever `python` resolves to - on Windows, `python3` usually does not exist.
$py = if (Get-Command py -ErrorAction SilentlyContinue) { "py" }
      elseif (Get-Command python -ErrorAction SilentlyContinue) { "python" }
      else { throw "Python not found on PATH. Install Python 3.11+ from python.org and tick 'Add to PATH'." }

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtualenv..."
    & $py -m venv .venv
}

# Windows venvs put executables in Scripts\, not bin/.
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "venv looks broken - delete .venv and re-run." }

& $venvPy -m pip install -q -r requirements.txt

# Headless Chromium for the animation templates (no-op once installed).
& $venvPy -m playwright install chromium

if (-not (Test-Path ".env")) {
    Write-Host "No .env found - copying .env.example. Add your keys before rendering."
    Copy-Item ".env.example" ".env"
}

# ffmpeg must be on PATH - the render pipeline shells out to it constantly.
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Warning "ffmpeg is not on PATH. Install it with:  winget install Gyan.FFmpeg"
    Write-Warning "then open a NEW terminal so the updated PATH is picked up."
}

if (-not $env:PORT) { $env:PORT = "5062" }
Write-Host ""
Write-Host "  >  Dmiqwi  ->  http://127.0.0.1:$($env:PORT)   (keep this terminal open)"
Write-Host ""
& $venvPy app.py
