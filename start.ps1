param(
    [switch]$NoTunnel,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$rootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $rootDir

# в”Ђв”Ђв”Ђ Check venv в”Ђв”Ђв”Ђ
if (-not (Test-Path "venv\Scripts\Activate.ps1")) {
    Write-Host "ERROR: venv not found. Run: python -m venv venv" -ForegroundColor Red
    exit 1
}

# в”Ђв”Ђв”Ђ Ollama в”Ђв”Ђв”Ђ
$ollamaRunning = $false
try {
    $resp = Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -Method GET -TimeoutSec 3 -ErrorAction Stop
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
            $resp = Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -Method GET -TimeoutSec 2 -ErrorAction Stop
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

# в”Ђв”Ђв”Ђ Uvicorn в”Ђв”Ђв”Ђ
Write-Host "Starting AITEACHER server..." -ForegroundColor Yellow
$uvicornJob = Start-Job -ScriptBlock {
    param($dir)
    Set-Location $dir
    . .\venv\Scripts\Activate.ps1
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
} -ArgumentList $rootDir

Start-Sleep -Seconds 3
$retry = 0
while ($retry -lt 15) {
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:8000" -Method GET -TimeoutSec 2 -ErrorAction Stop
        if ($resp.StatusCode -eq 200) { break }
    } catch {}
    $retry++
    Start-Sleep -Seconds 1
}
if ($retry -ge 15) {
    Write-Host "ERROR: Server did not start in 15 seconds" -ForegroundColor Red
    exit 1
}
Write-Host "AITEACHER server is running" -ForegroundColor Green

# в”Ђв”Ђв”Ђ Cloudflare Tunnel в”Ђв”Ђв”Ђ
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

# в”Ђв”Ђв”Ђ Show info в”Ђв”Ђв”Ђ
$localIp = & python get_ip.py 2>$null

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "     AITEACHER вЂ” Server is running" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Local:               http://localhost:8000" -ForegroundColor White
if ($localIp) {
    Write-Host "  LAN:                 http://$localIp`:8000" -ForegroundColor White
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

# в”Ђв”Ђв”Ђ Cleanup в”Ђв”Ђв”Ђ
Write-Host "Stopping processes..." -ForegroundColor Yellow
if ($cfProc -and -not $cfProc.HasExited) {
    Stop-Process -Id $cfProc.Id -Force -ErrorAction SilentlyContinue
}
if ($uvicornJob.State -eq "Running") {
    Stop-Job $uvicornJob -ErrorAction SilentlyContinue
    Remove-Job $uvicornJob -ErrorAction SilentlyContinue
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
