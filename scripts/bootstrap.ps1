# Подготовка локального окружения VK Monitor Assistant (Windows / PowerShell).
# Запуск:  powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
$ErrorActionPreference = "Stop"

$python = ".\.venv\Scripts\python.exe"

Write-Host "[1/5] Виртуальное окружение..." -ForegroundColor Cyan
if (-not (Test-Path $python)) {
    python -m venv .venv
}

Write-Host "[2/5] Установка зависимостей..." -ForegroundColor Cyan
& $python -m pip install --upgrade pip -q
& $python -m pip install -r requirements.txt

Write-Host "[3/5] Запуск PostgreSQL и Redis..." -ForegroundColor Cyan
docker compose up -d postgres redis

Write-Host "Ожидание готовности контейнеров..." -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds(60)
do {
    Start-Sleep -Seconds 2
    $running = docker compose ps --status running --format "{{.Service}}"
    if ($LASTEXITCODE -ne 0) { throw "docker compose ps завершился с ошибкой" }
} while (($running -notmatch "postgres") -and ((Get-Date) -lt $deadline))

Write-Host "[4/5] Применение миграций..." -ForegroundColor Cyan
& $python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw "alembic upgrade head failed" }

Write-Host "[5/5] Проверка окружения..." -ForegroundColor Cyan
& $python -m scripts.check_env
if ($LASTEXITCODE -ne 0) { throw "check_env failed" }

Write-Host ""
Write-Host "Окружение готово. Тесты: $python -m pytest tests" -ForegroundColor Green
Write-Host "Старт сервиса: $python -m app" -ForegroundColor Green
