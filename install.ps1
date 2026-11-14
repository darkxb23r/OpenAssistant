# Installs Jarvis on Windows, into .venv in this folder. Safe to run again (after an update, too).
#
#   powershell -ExecutionPolicy Bypass -File install.ps1               install or update
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Service      ... and start Jarvis at log-on
#   powershell -ExecutionPolicy Bypass -File install.ps1 -NoBrowser    ... without downloading Chromium now
#
# Needs: Python 3.11+, Node.js 20+, and Claude Code logged in with a Claude subscription.
param([switch]$Service, [switch]$NoBrowser)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Note($text) { Write-Host "   $text" }
function Fail($text) { Write-Host "`nInstall stopped: $text" -ForegroundColor Red; exit 1 }
function Run($exe, [string[]]$arguments, $what) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { Fail "$what failed (above)" }
}

Step "Python"
$python = $null
$candidates = @(@("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"), @("python"), @("python3"))
foreach ($c in $candidates) {
    if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
    $exe = & $c[0] @($c[1..9] + @("-c", "import sys; print(sys.executable) if sys.version_info >= (3, 11) else None")) 2>$null
    if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) { $python = $exe.Trim(); break }
}
if (-not $python) { Fail "Python 3.11 or newer wasn't found. Install it from https://www.python.org (tick 'Add to PATH')." }
Note "using $(& $python --version) ($python)"
$vpy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
# A .venv without pip (a run that stopped half way) is made again.
$venvOk = $false
if (Test-Path $vpy) { & $vpy -m pip --version *> $null; $venvOk = $LASTEXITCODE -eq 0 }
if (-not $venvOk) {
    if (Test-Path .venv) { Remove-Item -Recurse -Force .venv }
    Run $python @("-m", "venv", ".venv") "making the virtual environment"
}

Step "Python packages (a few minutes the first time)"
Run $vpy @("-m", "pip", "install", "--quiet", "--upgrade", "pip") "updating pip"
$gpuVersion = (Select-String -Path requirements.txt -Pattern '^onnxruntime-gpu\[[^\]]*\]==([0-9.]+)').Matches[0].Groups[1].Value
$gpu = $false
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { nvidia-smi -L *> $null; $gpu = $LASTEXITCODE -eq 0 }
if ($gpu) {
    Note "NVIDIA GPU found: installing the GPU speech engines"
    Run $vpy @("-m", "pip", "install", "--quiet", "-r", "requirements.txt") "installing the packages"
    # kokoro-onnx, openwakeword and faster-whisper pull in the CPU-only onnxruntime, which shares the
    # GPU build's files: take it out and put the GPU build back.
    & $vpy -m pip uninstall --quiet -y onnxruntime 2>$null
    Run $vpy @("-m", "pip", "install", "--quiet", "--force-reinstall", "--no-deps", "onnxruntime-gpu==$gpuVersion") "restoring the GPU speech engine"
} else {
    Note "no NVIDIA GPU: speech runs on the CPU (skipping several GB of CUDA libraries)"
    $cpu = Join-Path $PSScriptRoot ".venv\requirements-cpu.txt"
    (Get-Content requirements.txt | Where-Object { $_ -notmatch '^(nvidia-|onnxruntime-gpu)' }) + "onnxruntime" |
        Set-Content -Path $cpu -Encoding ascii
    Run $vpy @("-m", "pip", "install", "--quiet", "-r", $cpu) "installing the packages"
}

Step "Node.js and the browser interface"
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Fail "Node.js 20 or newer isn't installed (https://nodejs.org, or: winget install OpenJS.NodeJS.LTS)" }
node -e "process.exit(+process.versions.node.split('.')[0] < 20)"
if ($LASTEXITCODE -ne 0) { Fail "Node.js is $(node --version); 20 or newer is needed" }
Push-Location web
try {
    Run npm.cmd @("ci", "--no-audit", "--no-fund", "--loglevel=error") "installing the interface's packages"
    Run npm.cmd @("run", "build", "--silent") "building the browser interface"
} finally { Pop-Location }
Note "built web\dist"

if (-not $NoBrowser) {
    Step "Chromium for Jarvis's own browser"
    $pw = & $vpy -c "import browser_launch; print(browser_launch.PLAYWRIGHT_MCP)"
    npx.cmd -y $pw install-browser chromium *> $null
    if ($LASTEXITCODE -eq 0) { Note "ready" } else { Note "couldn't download it now; Jarvis tries again the first time he uses his browser" }
}

Step "Claude Code"
if (Get-Command claude -ErrorAction SilentlyContinue) {
    Note "found $(claude --version 2>$null | Select-Object -First 1). Log in once by running: claude"
} else {
    Note "not installed. Install it, then log in once:"
    Note "  npm install -g @anthropic-ai/claude-code; claude"
}
if ($env:ANTHROPIC_API_KEY) { Note "WARNING: ANTHROPIC_API_KEY is set. Jarvis refuses to start with it (it bills per token); remove it." }

Step "Settings folder and optional extras"
New-Item -ItemType Directory -Force -Path (Join-Path $HOME ".jarvis") | Out-Null
Note "secrets, memory, rules and logs live in $HOME\.jarvis (fill in secrets.env, or use the Settings panel)"
$cloudflared = Join-Path $env:LOCALAPPDATA "cloudflared\cloudflared.exe"
if (-not ((Test-Path $cloudflared) -or (Get-Command cloudflared -ErrorAction SilentlyContinue))) {
    Note "texts and calls need cloudflared (winget install Cloudflare.cloudflared)"
}

if ($Service) {
    Step "Service"
    Run $vpy @("jarvisctl.py", "install-service") "installing the log-on task"
}

Step "Done"
Note "start:      .venv\Scripts\python run_jarvis.py"
Note "pair a browser (in a second window): .venv\Scripts\python jarvisctl.py pair --name desktop"
Note "then open the address it prints. More in STARTUP.md."
