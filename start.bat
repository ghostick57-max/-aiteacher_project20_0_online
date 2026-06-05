@echo off
cd /d "%~dp0"

echo ============================================
echo    AITEACHER — Запуск
echo ============================================
echo.

:: Проверка PowerShell
where powershell >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Ошибка: PowerShell не найден
    pause
    exit /b 1
)

:: Запуск PowerShell-скрипта
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*

if %ERRORLEVEL% neq 0 (
    echo.
    pause
)
