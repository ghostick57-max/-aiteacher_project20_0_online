param(
    [switch]$NoTunnel,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$rootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $rootDir

if (-not (Test-Path "venv\Scripts\Activate.ps1")) {
    Write-Host "ERROR: venv not found. Run: python -m venv venv" -ForegroundColor Red
    exit 1
}

$ollamaRunning = $false
try {
    $resp = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:11434/api/tags" -Method GET -TimeoutSec 3 -ErrorAction Stop
    $ollamaRunning = $resp.StatusCode -eq 200
} catch {
    $ollamaRunning = $false
}

if (-not $ollamaRunning) {
    Write-Host "Starting Ollama..." -ForegroundColor Yellow
    $ollamaProc = Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 3
    $retry = 0
    while ($retry -lt 10) {
        try {
            $resp = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:11434/api/tags" -Method GET -TimeoutSec 2 -ErrorAction Stop
            if ($resp.StatusCode -eq 200) { break }
        } catch {}
        $retry++
        Start-Sleep -Seconds 1
    }
    if ($retry -ge 10) {
        Write-Host "ERROR: Ollama did not start. Run manually: ollama serve" -ForegroundColor Red
        exit 1
    }
    Write-Host "Ollama is running" -ForegroundColor Green
} else {
    Write-Host "Ollama already running" -ForegroundColor Green
    $ollamaProc = $null
}

Write-Host "Starting AITEACHER server..." -ForegroundColor Yellow
$uvicornOut = Join-Path $rootDir "uvicorn_out.log"
$uvicornErr = Join-Path $rootDir "uvicorn_err.log"
$activatePath = Join-Path $rootDir "venv\Scripts\Activate.ps1"
$mainPath = Join-Path $rootDir "main.py"
$uvicornProc = Start-Process -FilePath "powershell" -ArgumentList "-NoLogo -NoProfile -Command `". '$activatePath'; uvicorn main:app --host 0.0.0.0 --port 8000 --reload`"" -WindowStyle Hidden -PassThru -RedirectStandardOutput $uvicornOut -RedirectStandardError $uvicornErr

Start-Sleep -Seconds 5
$retry = 0
while ($retry -lt 30) {
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:8000" -Method GET -TimeoutSec 2 -ErrorAction Stop
        if ($resp.StatusCode -eq 200) { break }
    } catch {}
    $retry++
    Start-Sleep -Seconds 1
}
if ($retry -ge 30) {
    Write-Host "ERROR: Server did not start in 35 seconds." -ForegroundColor Red
    Write-Host "Check logs:" -ForegroundColor Yellow
    Write-Host "  stdout: $uvicornOut" -ForegroundColor Yellow
    Write-Host "  stderr: $uvicornErr" -ForegroundColor Yellow
    Get-Content -Path $uvicornErr -Tail 20 -ErrorAction SilentlyContinue
    exit 1
}
Write-Host "AITEACHER server is running" -ForegroundColor Green

$cfProc = $null
$publicUrl = $null
if (-not $NoTunnel) {
    $cfPath = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
    if (Test-Path $cfPath) {
        Write-Host "Starting Cloudflare Tunnel..." -ForegroundColor Yellow
        $cfLog = [System.IO.Path]::GetTempFileName()
        $cfProc = Start-Process -FilePath $cfPath -ArgumentList "tunnel --url http://localhost:8000" -WindowStyle Hidden -PassThru -RedirectStandardOutput $cfLog -RedirectStandardError $cfLog
        Start-Sleep -Seconds 5
        $retry = 0
        while ($retry -lt 20) {
            Start-Sleep -Seconds 1
            $logContent = Get-Content -Path $cfLog -Raw
            if ($logContent -match 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com') {
                $publicUrl = $matches[0]
                break
            }
            $retry++
        }
        if ($publicUrl) {
            Write-Host "Public tunnel is open" -ForegroundColor Green
        } else {
            Write-Host "Cloudflare Tunnel started, but URL not yet available. Check log: $cfLog" -ForegroundColor Yellow
        }
    } else {
        Write-Host "cloudflared not found at $cfPath. Install from: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/" -ForegroundColor Yellow
    }
}

$localIp = & python get_ip.py 2>$null

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "     AITEACHER - Server is running" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Local access:        http://localhost:8000" -ForegroundColor White
if ($localIp) {
    Write-Host "  LAN access:          http://$localIp`:8000" -ForegroundColor White
}
if ($publicUrl) {
    Write-Host "  Public (internet):   $publicUrl" -ForegroundColor Green
}
Write-Host "  Admin panel:         http://localhost:8000/admin" -ForegroundColor White
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

if (-not $NoBrowser) {
    Start-Process "http://localhost:8000"
}

Write-Host "Press Enter to stop all processes..." -ForegroundColor Yellow
$null = Read-Host

Write-Host "Stopping processes..." -ForegroundColor Yellow
if ($cfProc -and -not $cfProc.HasExited) {
    Stop-Process -Id $cfProc.Id -Force -ErrorAction SilentlyContinue
}
if ($uvicornProc -and -not $uvicornProc.HasExited) {
    Stop-Process -Id $uvicornProc.Id -Force -ErrorAction SilentlyContinue
    $uvicornChildren = Get-WmiObject Win32_Process -Filter "ParentProcessId=$($uvicornProc.Id)" 2>$null
    if ($uvicornChildren) {
        $uvicornChildren | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
}
if ($ollamaProc -and -not $ollamaProc.HasExited) {
    Write-Host "Stop Ollama too? (y/N): " -ForegroundColor Yellow -NoNewline
    $stopOllama = Read-Host
    if ($stopOllama -eq "y" -or $stopOllama -eq "Y") {
        Stop-Process -Id $ollamaProc.Id -Force -ErrorAction SilentlyContinue
        Write-Host "Ollama stopped" -ForegroundColor Green
    }
}
Write-Host "Server stopped" -ForegroundColor Green
