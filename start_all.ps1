$ErrorActionPreference = "Stop"

$MonitorRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$LocalCrmRoot = Join-Path $MonitorRoot "crm"
$CrmRoot = if (Test-Path $LocalCrmRoot) { $LocalCrmRoot } else { "C:\crm_inventory" }
$PostgresExe = "C:\pg17\pgsql\bin\postgres.exe"
$PostgresData = "C:\pg17\data"
$CrmPython = Join-Path $CrmRoot ".venv\Scripts\python.exe"
$DefaultPython = "python"

function Test-PortListening {
    param([int]$Port)

    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    return $null -ne $connection
}

function Get-PortOwner {
    param([int]$Port)

    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $connection) {
        return $null
    }
    return $connection.OwningProcess
}

function Get-CommandLineProcess {
    param([string]$Pattern)

    return Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like $Pattern } |
        Select-Object -First 1
}

function Start-PostgresIfNeeded {
    if (Test-PortListening -Port 5432) {
        $owner = Get-PortOwner -Port 5432
        Write-Host "PostgreSQL: already running on port 5432 (PID $owner)" -ForegroundColor Green
        return
    }

    if (-not (Test-Path $PostgresExe)) {
        Write-Host "PostgreSQL: not found at $PostgresExe" -ForegroundColor Red
        throw "PostgreSQL executable not found"
    }
    if (-not (Test-Path $PostgresData)) {
        Write-Host "PostgreSQL: data directory not found at $PostgresData" -ForegroundColor Red
        throw "PostgreSQL data directory not found"
    }

    Write-Host "PostgreSQL: starting..." -ForegroundColor Yellow
    Start-Process `
        -FilePath $PostgresExe `
        -ArgumentList @("-D", $PostgresData) `
        -WorkingDirectory (Split-Path -Parent $PostgresExe) `
        -RedirectStandardOutput (Join-Path $MonitorRoot "postgres_runtime.out.log") `
        -RedirectStandardError (Join-Path $MonitorRoot "postgres_runtime.err.log") `
        -WindowStyle Hidden

    Start-Sleep -Seconds 4
    if (-not (Test-PortListening -Port 5432)) {
        Write-Host "PostgreSQL: did not start, check postgres_runtime.err.log" -ForegroundColor Red
        throw "PostgreSQL did not start"
    }

    $owner = Get-PortOwner -Port 5432
    Write-Host "PostgreSQL: started (PID $owner)" -ForegroundColor Green
}

function Start-CrmIfNeeded {
    if (Test-PortListening -Port 8001) {
        $owner = Get-PortOwner -Port 8001
        Write-Host "CRM: already running on http://127.0.0.1:8001 (PID $owner)" -ForegroundColor Green
        return
    }

    if (-not (Test-Path $CrmRoot)) {
        Write-Host "CRM: folder not found at $CrmRoot" -ForegroundColor Red
        throw "CRM folder not found"
    }

    $pythonExe = if (Test-Path $CrmPython) { $CrmPython } else { $DefaultPython }

    Write-Host "CRM: starting..." -ForegroundColor Yellow
    Start-Process `
        -FilePath $pythonExe `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8001") `
        -WorkingDirectory $CrmRoot `
        -RedirectStandardOutput (Join-Path $CrmRoot "crm_runtime.out.log") `
        -RedirectStandardError (Join-Path $CrmRoot "crm_runtime.err.log") `
        -WindowStyle Hidden

    Start-Sleep -Seconds 5
    if (-not (Test-PortListening -Port 8001)) {
        Write-Host "CRM: did not start, check $(Join-Path $CrmRoot "crm_runtime.err.log")" -ForegroundColor Red
        throw "CRM did not start"
    }

    $owner = Get-PortOwner -Port 8001
    Write-Host "CRM: started on http://127.0.0.1:8001 (PID $owner)" -ForegroundColor Green
}

function Start-TelegramControlIfNeeded {
    $existing = Get-CommandLineProcess -Pattern "*telegram_control_bot.py*"
    if ($null -ne $existing) {
        Write-Host "Telegram panel: already running (PID $($existing.ProcessId))" -ForegroundColor Green
        return
    }

    Write-Host "Telegram panel: starting..." -ForegroundColor Yellow
    Start-Process `
        -FilePath $DefaultPython `
        -ArgumentList @("-u", "telegram_control_bot.py", "--announce") `
        -WorkingDirectory $MonitorRoot `
        -RedirectStandardOutput (Join-Path $MonitorRoot "telegram_control_bot.out.log") `
        -RedirectStandardError (Join-Path $MonitorRoot "telegram_control_bot.err.log") `
        -WindowStyle Hidden

    Start-Sleep -Seconds 4
    $started = Get-CommandLineProcess -Pattern "*telegram_control_bot.py*"
    if ($null -eq $started) {
        Write-Host "Telegram panel: did not start, check telegram_control_bot.err.log" -ForegroundColor Red
        throw "Telegram panel did not start"
    }

    Write-Host "Telegram panel: started (PID $($started.ProcessId))" -ForegroundColor Green
}

Write-Host ""
Write-Host "Starting Avito Monitor stack..." -ForegroundColor Cyan
Write-Host ""

Start-PostgresIfNeeded
Start-CrmIfNeeded
Start-TelegramControlIfNeeded

Write-Host ""
Write-Host "Ready." -ForegroundColor Cyan
Write-Host "Open AdsPower profile manually, then use Telegram buttons to set link/start search." -ForegroundColor Cyan
Write-Host "CRM: http://127.0.0.1:8001/market" -ForegroundColor Cyan
