param(
    [switch]$NoTunnel,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$rootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $rootDir

# ─── Проверка venv ───
if (-not (Test-Path "venv\Scripts\Activate.ps1")) {
    Write-Host "Ошибка: venv не найден. Сначала выполните: python -m venv venv" -ForegroundColor Red
    exit 1
}

# ─── Ollama ───
$ollamaRunning = $false
try {
    $resp = Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -Method GET -TimeoutSec 3 -ErrorAction Stop
    $ollamaRunning = $resp.StatusCode -eq 200
} catch {
    $ollamaRunning = $false
}

if (-not $ollamaRunning) {
    Write-Host "Запуск Ollama..." -ForegroundColor Yellow
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
        Write-Host "Ошибка: Ollama не запустилась. Запустите вручную: ollama serve" -ForegroundColor Red
        exit 1
    }
    Write-Host "Ollama запущена" -ForegroundColor Green
} else {
    Write-Host "Ollama уже запущена" -ForegroundColor Green
    $ollamaProc = $null
}

# ─── Uvicorn ───
Write-Host "Запуск AITEACHER сервера..." -ForegroundColor Yellow
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
    Write-Host "Ошибка: сервер не запустился за 15 секунд" -ForegroundColor Red
    exit 1
}
Write-Host "Сервер AITEACHER запущен" -ForegroundColor Green

# ─── Cloudflare Tunnel ───
$cfProc = $null
$publicUrl = $null
if (-not $NoTunnel) {
    $cfPath = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
    if (Test-Path $cfPath) {
        Write-Host "Запуск Cloudflare Tunnel..." -ForegroundColor Yellow
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
            Write-Host "Публичный туннель открыт" -ForegroundColor Green
        } else {
            Write-Host "Cloudflare Tunnel запущен, но URL ещё не получен. Проверьте лог: $cfLog" -ForegroundColor Yellow
        }
    } else {
        Write-Host "cloudflared не найден по пути $cfPath. Установите: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/" -ForegroundColor Yellow
    }
}

# ─── Вывод информации ───
$localIp = & python get_ip.py 2>$null

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "     AITEACHER — Сервер запущен" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Локальный доступ:     http://localhost:8000" -ForegroundColor White
if ($localIp) {
    Write-Host "  Локальная сеть:      http://$localIp`:8000" -ForegroundColor White
}
if ($publicUrl) {
    Write-Host "  Публичный доступ:    $publicUrl" -ForegroundColor Green
}
Write-Host "  Админ-панель:        http://localhost:8000/admin" -ForegroundColor White
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

if (-not $NoBrowser) {
    Start-Process "http://localhost:8000"
}

Write-Host "Нажмите Enter для остановки всех процессов..." -ForegroundColor Yellow
$null = Read-Host

# ─── Cleanup ───
Write-Host "Остановка процессов..." -ForegroundColor Yellow
if ($cfProc -and -not $cfProc.HasExited) {
    Stop-Process -Id $cfProc.Id -Force -ErrorAction SilentlyContinue
}
if ($uvicornJob.State -eq "Running") {
    Stop-Job $uvicornJob -ErrorAction SilentlyContinue
    Remove-Job $uvicornJob -ErrorAction SilentlyContinue
}
if ($ollamaProc -and -not $ollamaProc.HasExited) {
    Write-Host "Хотите остановить Ollama? (y/N): " -ForegroundColor Yellow -NoNewline
    $stopOllama = Read-Host
    if ($stopOllama -eq "y" -or $stopOllama -eq "Y") {
        Stop-Process -Id $ollamaProc.Id -Force -ErrorAction SilentlyContinue
        Write-Host "Ollama остановлена" -ForegroundColor Green
    }
}
Write-Host "Сервер остановлен" -ForegroundColor Green
