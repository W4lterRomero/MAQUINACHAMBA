<#
.SYNOPSIS
    Maquina de Chamba - Script Maestro de Automatizacion y Operacion para Windows.

.DESCRIPTION
    Proporciona comandos nativos de PowerShell para instalar, configurar, ejecutar,
    supervisar, programar en segundo plano mediante el Programador de Tareas de Windows,
    y realizar pruebas de extremo a extremo sin requerir ningun IDE abierto.

.PARAMETER Command
    Subcomando a ejecutar:
    - install   : Instala dependencias en .venv, configura .env y aplica migraciones.
    - start     : Inicia el servidor web (FastAPI/Uvicorn) y el demonio en segundo plano.
    - stop      : Detiene los procesos en ejecucion de forma segura.
    - restart   : Reinicia el servidor y el demonio.
    - status    : Comprueba el estado de salud, procesos activos, zona horaria y simulacion.
    - schedule  : Registra la tarea persistente en el Programador de Tareas de Windows.
    - unschedule: Elimina la tarea del Programador de Tareas de Windows.
    - test      : Ejecuta la suite completa de pruebas automatizadas (pytest).
    - demo      : Ejecuta la demostracion de 12 pasos obligatorios de extremo a extremo.

.EXAMPLE
    .\scripts\maquinachamba.ps1 install
    .\scripts\maquinachamba.ps1 start
    .\scripts\maquinachamba.ps1 status
    .\scripts\maquinachamba.ps1 demo
    .\scripts\maquinachamba.ps1 stop
#>

param(
    [Parameter(Position = 0, Mandatory = $false)]
    [string]$Command = "status"
)

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$PidDir = Join-Path $RepoRoot ".pids"
$ServerPidFile = Join-Path $PidDir "server.pid"
$DaemonPidFile = Join-Path $PidDir "daemon.pid"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$TaskName = "MaquinaChambaAutoService"

if (-not (Test-Path $PidDir)) {
    New-Item -ItemType Directory -Path $PidDir -Force | Out-Null
}

function Write-Banner {
    param([string]$Title)
    Write-Host ""
    Write-Host ("=" * 70) -ForegroundColor Cyan
    Write-Host "  MAQUINA DE CHAMBA - $Title" -ForegroundColor Yellow
    Write-Host ("=" * 70) -ForegroundColor Cyan
    Write-Host ""
}

function Ensure-EnvConfiguration {
    $EnvFile = Join-Path $RepoRoot ".env"
    $EnvExample = Join-Path $RepoRoot ".env.example"
    if (-not (Test-Path $EnvFile)) {
        Write-Host "[+] Creando archivo .env desde .env.example..." -ForegroundColor Green
        Copy-Item -Path $EnvExample -Destination $EnvFile
    }
    $raw = Get-Content $EnvFile -Raw
    if ($raw -notmatch "APP_TIMEZONE=") {
        Add-Content -Path $EnvFile -Value "`nAPP_TIMEZONE=America/El_Salvador"
    }
    if ($raw -notmatch "EXECUTION_TIMEZONE=") {
        Add-Content -Path $EnvFile -Value "`nEXECUTION_TIMEZONE=America/El_Salvador"
    }
    if ($raw -notmatch "SIMULATION_MODE=") {
        Add-Content -Path $EnvFile -Value "`nSIMULATION_MODE=true"
    }
}

function Invoke-Install {
    Write-Banner "Instalacion y Configuracion Nativa en Windows"

    if (-not (Test-Path $VenvPython)) {
        Write-Host "[*] Entorno virtual no detectado en .venv. Creando..." -ForegroundColor Yellow
        $SysPy = Get-Command "python" -ErrorAction SilentlyContinue
        if ($SysPy) {
            & python -m venv (Join-Path $RepoRoot ".venv")
        } else {
            $Uv = Get-Command "uv" -ErrorAction SilentlyContinue
            if ($Uv) {
                & uv venv (Join-Path $RepoRoot ".venv")
            } else {
                Write-Error "No se encontro Python ni uv en el sistema. Instala Python 3.12 para continuar."
                return
            }
        }
    }

    Write-Host "[+] Instalando dependencias desde backend/requirements.txt..." -ForegroundColor Green
    & $VenvPython -m pip install --quiet --upgrade pip
    & $VenvPython -m pip install --quiet -r (Join-Path $RepoRoot "backend\requirements.txt")
    & $VenvPython -m pip install --quiet "graphifyy==0.9.2"
    Write-Host "[+] Instalando navegadores de Playwright (Chromium)..." -ForegroundColor Green
    & $VenvPython -m playwright install chromium

    Ensure-EnvConfiguration

    Write-Host "[+] Aplicando migraciones de base de datos..." -ForegroundColor Green
    & $VenvPython -m alembic upgrade head

    Write-Host "[+] Verificando seguridad de secretos..." -ForegroundColor Green
    & $VenvPython (Join-Path $RepoRoot "scripts\check_secrets.py") backend/app scripts

    Write-Host ""
    Write-Host "[OK] Instalacion completada exitosamente." -ForegroundColor Green
    Write-Host "     Zona Horaria inicial: America/El_Salvador" -ForegroundColor White
    Write-Host "     Modo: SIMULACION ACTIVA (Garantia de no envios accidentales)" -ForegroundColor Yellow
}

function Invoke-Start {
    Write-Banner "Iniciando Servicios en Windows"

    if (-not (Test-Path $VenvPython)) {
        Write-Host "[*] El entorno no esta instalado. Ejecutando instalacion previa..." -ForegroundColor Yellow
        Invoke-Install
    }

    Ensure-EnvConfiguration

    # 1. Servidor Web FastAPI
    $ServerRunning = $false
    $conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) {
        Write-Host "[*] El servidor web ya esta en ejecucion en el puerto 8000 (PID: $($conn.OwningProcess))." -ForegroundColor Yellow
        Set-Content -Path $ServerPidFile -Value $conn.OwningProcess
        $ServerRunning = $true
    } elseif (Test-Path $ServerPidFile) {
        $oldPid = (Get-Content $ServerPidFile -Raw).Trim()
        if ($oldPid -and (Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue)) {
            Write-Host "[*] El servidor web ya esta en ejecucion (PID: $oldPid)." -ForegroundColor Yellow
            $ServerRunning = $true
        } else {
            Remove-Item -Path $ServerPidFile -Force -ErrorAction SilentlyContinue
        }
    }

    if (-not $ServerRunning) {
        Write-Host "[+] Iniciando servidor web FastAPI en http://127.0.0.1:8000 ..." -ForegroundColor Green
        $serverLog = Join-Path $PidDir "server.log"
        $serverErr = Join-Path $PidDir "server_err.log"
        $argsServer = @("-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", "8000")
        $serverProc = Start-Process -FilePath $VenvPython -ArgumentList $argsServer -WorkingDirectory $RepoRoot -RedirectStandardOutput $serverLog -RedirectStandardError $serverErr -WindowStyle Hidden -PassThru
        Set-Content -Path $ServerPidFile -Value $serverProc.Id
        Write-Host "[OK] Servidor iniciado con PID $($serverProc.Id)." -ForegroundColor Green
    }

    # 2. Demonio de Ejecucion
    $DaemonRunning = $false
    if (Test-Path $DaemonPidFile) {
        $oldPid = (Get-Content $DaemonPidFile -Raw).Trim()
        if ($oldPid -and (Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue)) {
            Write-Host "[*] El demonio de ejecucion ya esta activo (PID: $oldPid)." -ForegroundColor Yellow
            $DaemonRunning = $true
        } else {
            Remove-Item -Path $DaemonPidFile -Force -ErrorAction SilentlyContinue
        }
    }

    if (-not $DaemonRunning) {
        Write-Host "[+] Iniciando demonio de ejecucion durable en segundo plano..." -ForegroundColor Green
        $daemonLog = Join-Path $PidDir "daemon.log"
        $daemonErr = Join-Path $PidDir "daemon_err.log"
        $daemonScript = "scripts\run_execution_daemon.py"
        $daemonProc = Start-Process -FilePath $VenvPython -ArgumentList @($daemonScript) -WorkingDirectory $RepoRoot -RedirectStandardOutput $daemonLog -RedirectStandardError $daemonErr -WindowStyle Hidden -PassThru
        Set-Content -Path $DaemonPidFile -Value $daemonProc.Id
        Write-Host "[OK] Demonio iniciado con PID $($daemonProc.Id)." -ForegroundColor Green
    }

    Start-Sleep -Seconds 2
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health" -Method Get -TimeoutSec 4
        Write-Host ""
        Write-Host "============================================================" -ForegroundColor Green
        Write-Host "  MAQUINA DE CHAMBA OPERATIVA Y ACTIVA" -ForegroundColor Green
        Write-Host "  URL: http://127.0.0.1:8000/" -ForegroundColor White
        Write-Host "  Estado: $($health.status) | Modo: $($health.mode)" -ForegroundColor Yellow
        Write-Host "  Zona Horaria: America/El_Salvador" -ForegroundColor White
        Write-Host "============================================================" -ForegroundColor Green
    } catch {
        Write-Host "[+] Accede a http://127.0.0.1:8000/ en tu navegador." -ForegroundColor Cyan
    }
}

function Invoke-Stop {
    Write-Banner "Deteniendo Servicios"

    if (Test-Path $ServerPidFile) {
        $pidVal = (Get-Content $ServerPidFile -Raw).Trim()
        if ($pidVal) {
            $proc = Get-Process -Id ([int]$pidVal) -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host "[*] Deteniendo servidor web (PID: $pidVal)..." -ForegroundColor Yellow
                Stop-Process -Id ([int]$pidVal) -Force -ErrorAction SilentlyContinue
            }
        }
        Remove-Item -Path $ServerPidFile -Force -ErrorAction SilentlyContinue
    }

    $conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) {
        Write-Host "[*] Liberando puerto 8000 (PID: $($conn.OwningProcess))..." -ForegroundColor Yellow
        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
    }

    if (Test-Path $DaemonPidFile) {
        $pidVal = (Get-Content $DaemonPidFile -Raw).Trim()
        if ($pidVal) {
            $proc = Get-Process -Id ([int]$pidVal) -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host "[*] Deteniendo demonio de ejecucion (PID: $pidVal)..." -ForegroundColor Yellow
                Stop-Process -Id ([int]$pidVal) -Force -ErrorAction SilentlyContinue
            }
        }
        Remove-Item -Path $DaemonPidFile -Force -ErrorAction SilentlyContinue
    }

    Write-Host "[OK] Procesos detenidos correctamente." -ForegroundColor Green
}

function Invoke-Status {
    Write-Banner "Estado de Salud y Procesos Locales"

    $serverOk = $false
    $conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) {
        Write-Host "  [ACTIVO]   Servidor Web (PID: $($conn.OwningProcess)) en puerto 8000" -ForegroundColor Green
        Set-Content -Path $ServerPidFile -Value $conn.OwningProcess
        $serverOk = $true
    } elseif (Test-Path $ServerPidFile) {
        $sPid = (Get-Content $ServerPidFile -Raw).Trim()
        if ($sPid -and (Get-Process -Id ([int]$sPid) -ErrorAction SilentlyContinue)) {
            Write-Host "  [ACTIVO]   Servidor Web (PID: $sPid)" -ForegroundColor Green
            $serverOk = $true
        } else {
            Write-Host "  [INACTIVO] Servidor Web (PID no activo)" -ForegroundColor Gray
            Remove-Item -Path $ServerPidFile -Force -ErrorAction SilentlyContinue
        }
    } else {
        Write-Host "  [INACTIVO] Servidor Web" -ForegroundColor Gray
    }

    if (Test-Path $DaemonPidFile) {
        $dPid = (Get-Content $DaemonPidFile -Raw).Trim()
        if ($dPid -and (Get-Process -Id ([int]$dPid) -ErrorAction SilentlyContinue)) {
            Write-Host "  [ACTIVO]   Demonio de Ejecucion (PID: $dPid)" -ForegroundColor Green
        } else {
            Write-Host "  [INACTIVO] Demonio de Ejecucion" -ForegroundColor Gray
            Remove-Item -Path $DaemonPidFile -Force -ErrorAction SilentlyContinue
        }
    } else {
        Write-Host "  [INACTIVO] Demonio de Ejecucion" -ForegroundColor Gray
    }

    # Comprobar mecanismos de persistencia en Windows
    $startupVbs = Join-Path ([Environment]::GetFolderPath("Startup")) "MaquinaChamba.vbs"
    $hasStartup = Test-Path $startupVbs
    $null = cmd /c "schtasks.exe /Query /TN `"$TaskName`" 2>nul"
    $hasSchTask = ($LASTEXITCODE -eq 0)

    if ($hasStartup -or $hasSchTask) {
        $details = @()
        if ($hasStartup) { $details += "Inicio Automatico Windows (Startup)" }
        if ($hasSchTask) { $details += "Task Scheduler Diario (08:00)" }
        Write-Host "  [ACTIVA]   Persistencia Windows: REGISTRADA ($($details -join ', '))" -ForegroundColor Cyan
    } else {
        Write-Host "  [INACTIVA] Persistencia Windows: No programada ('schedule' para habilitar)" -ForegroundColor Gray
    }

    if ($serverOk) {
        try {
            $h = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health" -Method Get -TimeoutSec 3
            Write-Host "  [OK]       Endpoint /api/health: Respondio $($h.status) (Modo: $($h.mode))" -ForegroundColor Green
            Write-Host "             Simulacion: $($h.simulation_mode) | DB: $($h.database)" -ForegroundColor White
        } catch {
            Write-Host "  [AVISO]    Endpoint /api/health no respondio en timeout" -ForegroundColor Yellow
        }
    }

    Write-Host ""
    Write-Host "  Panel Web: http://127.0.0.1:8000/" -ForegroundColor White
}

function Invoke-Schedule {
    Write-Banner "Programacion Persistente en Windows (Task Scheduler + Inicio Automatico)"

    $scriptFullPath = $MyInvocation.MyCommand.Path
    $startupFolder = [Environment]::GetFolderPath("Startup")
    $vbsPath = Join-Path $startupFolder "MaquinaChamba.vbs"

    Write-Host "[+] Configurando inicio automatico al iniciar sesion en Windows..." -ForegroundColor Green
    $vbsContent = "Set WshShell = CreateObject(`"WScript.Shell`")`r`nWshShell.Run `"powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File `"`"$scriptFullPath`"`" start`", 0, False`r`n"
    [System.IO.File]::WriteAllText($vbsPath, $vbsContent, [System.Text.Encoding]::ASCII)
    Write-Host "    [OK] Creado lanzador silencioso en: $vbsPath" -ForegroundColor White

    Write-Host "[+] Registrando tarea de respaldo en Task Scheduler (Diario 08:00)..." -ForegroundColor Green
    $actionString = "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptFullPath`" start"
    $null = cmd /c "schtasks.exe /Create /TN `"$TaskName`" /TR `"$actionString`" /SC DAILY /ST 08:00 /F 2>nul"
    if ($LASTEXITCODE -eq 0) {
        Write-Host "    [OK] Tarea diaria 'MaquinaChambaAutoService' registrada en Task Scheduler." -ForegroundColor White
    }

    Write-Host ""
    Write-Host "[OK] Persistencia en Windows completada exitosamente." -ForegroundColor Green
    Write-Host "     Maquina de Chamba se iniciara en segundo plano al encender el equipo o iniciar sesion," -ForegroundColor White
    Write-Host "     sin depender de que Antigravity ni ninguna consola permanezca abierta." -ForegroundColor White
}

function Invoke-Unschedule {
    Write-Banner "Eliminar Persistencia de Windows"

    $startupFolder = [Environment]::GetFolderPath("Startup")
    $vbsPath = Join-Path $startupFolder "MaquinaChamba.vbs"
    if (Test-Path $vbsPath) {
        Remove-Item -Path $vbsPath -Force -ErrorAction SilentlyContinue
        Write-Host "[OK] Lanzador de Inicio Automatico eliminado ($vbsPath)." -ForegroundColor Green
    }

    $null = cmd /c "schtasks.exe /Delete /TN `"$TaskName`" /F 2>nul"
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[OK] Tarea programada '$TaskName' eliminada de Task Scheduler." -ForegroundColor Green
    } else {
        Write-Host "[*] Tarea de Task Scheduler no estaba registrada." -ForegroundColor Yellow
    }
}

function Invoke-Tests {
    Write-Banner "Ejecutando Suite de Pruebas Automatizadas (Pytest)"
    & $VenvPython -m pytest (Join-Path $RepoRoot "tests") -v
}

function Invoke-Demo {
    Write-Banner "Demostracion de 12 Pasos Obligatorios (Extremo a Extremo)"
    & $VenvPython (Join-Path $RepoRoot "scripts\demo_windows_chatgpt_gemini.py")
}

$cmdLower = $Command.ToLower()
if ($cmdLower -eq "install" -or $cmdLower -eq "setup") {
    Invoke-Install
} elseif ($cmdLower -eq "start") {
    Invoke-Start
} elseif ($cmdLower -eq "stop") {
    Invoke-Stop
} elseif ($cmdLower -eq "restart") {
    Invoke-Stop
    Start-Sleep -Seconds 1
    Invoke-Start
} elseif ($cmdLower -eq "status") {
    Invoke-Status
} elseif ($cmdLower -eq "schedule") {
    Invoke-Schedule
} elseif ($cmdLower -eq "unschedule") {
    Invoke-Unschedule
} elseif ($cmdLower -eq "test") {
    Invoke-Tests
} elseif ($cmdLower -eq "demo") {
    Invoke-Demo
} else {
    Invoke-Status
}
