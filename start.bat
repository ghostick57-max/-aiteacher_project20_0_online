@echo off
cd /d "%~dp0"

echo Проверка venv...
if not exist venv\Scripts\activate (
    echo Ошибка: venv не найден. Сначала выполните: python -m venv venv
    pause
    exit /b 1
)

echo Активация окружения...
call venv\Scripts\activate

:: Получение локального IPv4-адреса
for /f "tokens=* delims=" %%i in ('python get_ip.py') do set LOCAL_IP=%%i

echo.
echo ============================================
echo    AITEACHER — Сервер запущен
echo ============================================
echo     Локальный доступ:  http://localhost:8000
if not "%LOCAL_IP%"=="" echo     Другие устройства: http://%LOCAL_IP%:8000
echo     Админ-панель:     http://localhost:8000/admin
if not "%LOCAL_IP%"=="" echo     Админ-панель:     http://%LOCAL_IP%:8000/admin
echo ============================================
echo.

echo Запуск сервера AITEACHER...
start "" "http://localhost:8000/admin"
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

pause
