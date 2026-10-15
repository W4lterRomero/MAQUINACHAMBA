#!/usr/bin/env python3
"""
Demostración reproducible completa de Máquina de Chamba (Fase 1).
Ejecuta el recorrido completo de interfaz, persistencia SQLite y demonio
autónomo como procesos reales del sistema operativo, sin requerir IDE ni servicios externos.

Acciones demostradas:
1. Servidor web real en subproceso independiente (uvicorn).
2. Captura de evidencia del navegador (Google Chrome Headless: DOM + screenshot).
3. Recorrido completo del usuario: Registro -> Perfil -> CV -> Vacante -> Evaluación -> Preparación -> Revisión -> Aprobación -> CRM.
4. Servicio programado autónomo en subproceso separado (run_execution_daemon.py).
5. Demostración de crash & restart: caída abrupta y recuperación segura sin duplicar despacho.
6. Verificación directa de persistencia de tablas en SQLite temporal.
"""

import asyncio
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent


def find_free_port() -> int:
    """Encuentra un puerto TCP local disponible."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_chrome_headless(url: str, screenshot_path: Path, dom_path: Path) -> bool:
    """Ejecuta Google Chrome en modo headless para capturar DOM y captura de pantalla."""
    chrome_bin = shutil.which("google-chrome") or shutil.which("chromium-browser") or shutil.which("chromium")
    if not chrome_bin:
        print("  [AVISO] Google Chrome no encontrado en el PATH del sistema.")
        return False

    cmd = [
        chrome_bin,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--window-size=1280,900",
        f"--screenshot={screenshot_path}",
        "--dump-dom",
        url,
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        if res.returncode == 0:
            dom_path.write_text(res.stdout, encoding="utf-8")
            return True
        else:
            print(f"  [AVISO] Chrome finalizó con código {res.returncode}: {res.stderr[:200]}")
            return False
    except Exception as exc:
        print(f"  [AVISO] Error al ejecutar Chrome headless: {exc}")
        return False


def main():
    print("=" * 75)
    print(" DEMOSTRACIÓN E2E: RECORRIDO DE INTERFAZ Y SERVICIO AUTÓNOMO SIN IDE")
    print(" Máquina de Chamba — Fase 1 (Modo Simulación Estricto)")
    print("=" * 75)

    # 1. Crear entorno temporal completamente aislado
    demo_dir = Path(tempfile.mkdtemp(prefix="maquinachamba_demo_"))
    db_file = demo_dir / "demo_maquinachamba.db"
    secret_file = demo_dir / ".secret_key"
    screenshots_dir = demo_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    print(f"\n[1/6] Configuración del entorno temporal:")
    print(f"  - Directorio temporal: {demo_dir}")
    print(f"  - Base de datos SQLite: {db_file}")
    print(f"  - Archivo de clave protegida: {secret_file}")
    print(f"  - Puerto de API local: {port}")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file}"
    env["APP_SECRET_PATH"] = str(secret_file)
    env["APP_PORT"] = str(port)
    env["APP_HOST"] = "127.0.0.1"
    env["SIMULATION_MODE"] = "true"
    env["APP_ENV"] = "development"

    server_proc = None
    try:
        # 2. Inicializar esquema de base de datos en SQLite temporal
        print("\n[2/6] Inicializando base de datos SQLite temporal con Base.metadata...")
        init_script = (
            "import asyncio\n"
            "from backend.app.core.database import init_db, close_db\n"
            "import backend.app.modules.users.models\n"
            "import backend.app.modules.profile.models\n"
            "import backend.app.modules.cv.models\n"
            "import backend.app.modules.vacancies.models\n"
            "import backend.app.modules.evaluation.models\n"
            "import backend.app.modules.approvals.models\n"
            "import backend.app.modules.crm.models\n"
            "asyncio.run(init_db())\n"
            "asyncio.run(close_db())\n"
        )
        res_init = subprocess.run([sys.executable, "-c", init_script], env=env, capture_output=True, text=True)
        assert res_init.returncode == 0, f"Fallo al inicializar base de datos: {res_init.stderr}"
        print("  Base de datos SQLite inicializada correctamente.")

        # 3. Lanzar servidor web real en proceso separado
        print(f"\n[3/6] Iniciando proceso real de servidor web (uvicorn en PID separado)...")
        server_log = demo_dir / "uvicorn.log"
        with open(server_log, "w", encoding="utf-8") as log_out:
            server_proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                env=env,
                stdout=log_out,
                stderr=subprocess.STDOUT,
            )

        print(f"  Servidor iniciado con PID {server_proc.pid}. Esperando disponibilidad...")
        # Esperar a que el servidor responda /health
        ready = False
        with httpx.Client(timeout=1.0) as client:
            for _ in range(50):
                try:
                    r = client.get(f"{base_url}/api/health")
                    if r.status_code == 200:
                        ready = True
                        break
                except Exception:
                    time.sleep(0.1)

        if not ready:
            print("--- LOG DE UVICORN ---")
            print(server_log.read_text(encoding="utf-8"))
            print("-----------------------")
        assert ready, "El servidor uvicorn no estuvo listo a tiempo."
        print(f"  Servidor respondiendo OK en {base_url}/health.")

        # Evidencia del navegador (Chrome Headless) cargando la interfaz inicial
        scr1 = screenshots_dir / "01_browser_landing.png"
        dom1 = demo_dir / "01_browser_landing.html"
        print(f"\n[4/6] Capturando evidencia del navegador real con Google Chrome Headless...")
        chrome_ok = run_chrome_headless(f"{base_url}/app", scr1, dom1)
        if chrome_ok:
            print(f"  Captura de pantalla guardada: {scr1} ({scr1.stat().st_size} bytes)")
            print(f"  DOM HTML extraído: {dom1} ({dom1.stat().st_size} bytes)")
            assert "Máquina de Chamba" in dom1.read_text(encoding="utf-8"), "El DOM no contiene el título esperado"
            print("  Verificado: la interfaz carga limpiamente en el navegador.")

        # 4. Recorrido completo del usuario a través de la API
        print(f"\n[5/6] Ejecutando el recorrido completo de usuario (Registro -> Aprobación -> CRM)...")
        with httpx.Client(base_url=base_url, timeout=10.0) as client:
            # Paso A: Registro público
            reg_res = client.post("/api/v1/auth/register", json={
                "email": "candidato_demo@maquinachamba.local",
                "password": "PasswordDemo2026!",
                "full_name": "Candidato Demostración",
            })
            assert reg_res.status_code == 201, f"Fallo en registro: {reg_res.text}"
            token = reg_res.json()["access_token"]
            tenant_id = reg_res.json()["tenant_id"]
            headers = {"Authorization": f"Bearer {token}"}
            print(f"  [A] Usuario registrado con éxito. Tenant asignado por servidor: {tenant_id}")

            # Paso B: Configuración del Perfil
            prof_res = client.post("/api/v1/profile", json={
                "full_name": "Candidato Demostración",
                "email": "candidato_demo@maquinachamba.local",
                "location_city": "Ciudad de México",
                "location_country": "México",
                "availability": "immediate",
                "job_preferences": {"role": "Senior Backend Developer", "modality": "remote"},
            }, headers=headers)
            assert prof_res.status_code == 200, f"Fallo en perfil: {prof_res.text}"
            print("  [B] Perfil profesional configurado y persistido.")

            # Paso C: Creación de CV Maestro
            cv_res = client.post("/api/v1/profile/cv", json={
                "name": "CV Backend Python Principal",
                "specialization": "Python / FastAPI / SQLite / Arquitectura",
                "summary": "Ingeniero backend con experiencia en sistemas distribuidos y alta resiliencia.",
            }, headers=headers)
            assert cv_res.status_code == 201, f"Fallo en CV: {cv_res.text}"
            cv_id = cv_res.json()["id"]
            print(f"  [C] CV Maestro creado con ID: {cv_id}")

            # Paso D: Descubrimiento e Importación de Vacante
            vac_res = client.post("/api/v1/vacancies", json={
                "company_name": "Tech Corp Innovación",
                "job_title": "Senior Python Architect",
                "source_url": "https://techcorp.example/careers/job-9874",
                "location": "Remoto (América)",
                "requirements": ["Python", "FastAPI", "SQLite", "Resiliencia"],
                "description": "Vacante para liderar arquitectura backend local-first.",
            }, headers=headers)
            assert vac_res.status_code == 201, f"Fallo en vacante: {vac_res.text}"
            vac_id = vac_res.json()["id"]
            print(f"  [D] Vacante importada con validación anti-SSRF con ID: {vac_id}")

            # Paso E: Evaluación de Compatibilidad
            eval_res = client.post("/api/v1/evaluations", json={
                "vacancy_id": vac_id,
                "cv_id": cv_id,
            }, headers=headers)
            assert eval_res.status_code == 201, f"Fallo en evaluación: {eval_res.text}"
            eval_id = eval_res.json()["id"]
            score = eval_res.json()["compatibility_score"]
            print(f"  [E] Evaluación calculada deterministamente (score={score}, ID={eval_id})")

            # Paso F: Preparación del Paquete de Candidatura
            prep_res = client.post("/api/v1/candidacies/prepare", json={
                "vacancy_id": vac_id,
                "cv_id": cv_id,
                "evaluation_id": eval_id,
                "cover_letter": "Estimado equipo de Tech Corp,\nPresento mi postulación para el rol de Senior Python Architect...",
            }, headers=headers)
            assert prep_res.status_code == 201, f"Fallo en preparación: {prep_res.text}"
            cand_id = prep_res.json()["id"]
            print(f"  [F] Paquete de candidatura preparado con ID: {cand_id}")

            # Paso G: Inspección / Revisión Humana previa a la aprobación
            cand_view = client.get(f"/api/v1/candidacies/{cand_id}", headers=headers)
            assert cand_view.status_code == 200, f"Fallo en inspección: {cand_view.text}"
            cand_data = cand_view.json()
            content_hash = cand_data["candidacy"]["content_hash"]
            print(f"  [G] Inspección de paquete: Hash SHA-256 inmutable generado: {content_hash[:16]}...")

            # Paso H: Aprobación del Usuario
            app_res = client.post(f"/api/v1/candidacies/{cand_id}/approve", json={
                "notes": "Aprobado tras revisión de vacante, CV adaptado y carta de presentación.",
            }, headers=headers)
            assert app_res.status_code == 200, f"Fallo en aprobación: {app_res.text}"
            approval_id = app_res.json()["approval_id"]
            print(f"  [H] Decisión inmutable de aprobación registrada (ID={approval_id}). Tarea programada para mañana.")

            # Paso I: Consulta del CRM Kanban
            crm_res = client.get("/api/v1/crm/entries", headers=headers)
            assert crm_res.status_code == 200
            entries = crm_res.json()
            assert len(entries) == 1
            print(f"  [I] Tablero CRM consultado: Entrada en estado '{entries[0]['current_status']}' con prioridad '{entries[0]['priority']}'.")

        # Evidencia del navegador post-aprobación
        scr2 = screenshots_dir / "02_browser_crm_approved.png"
        dom2 = demo_dir / "02_browser_crm_approved.html"
        run_chrome_headless(f"{base_url}/app", scr2, dom2)

        # 5. Demostración del Servicio de Ejecución Autónomo como Proceso Real Separado
        print(f"\n[6/6] Demostración del launcher de ejecución durable (scripts/run_execution_daemon.py)...")

        # Modificar en SQLite la fecha e instante de la tarea para que sea inmediatamente vencida dentro de ventana
        con = sqlite3.connect(db_file)
        cur = con.cursor()
        now_dt = datetime.now()
        cur.execute(
            "UPDATE scheduled_tasks SET scheduled_date = ?, scheduled_time = '00:00:00', execution_instant_utc = '2020-01-01 00:00:00'",
            (now_dt.strftime("%Y-%m-%d"),)
        )
        con.commit()
        con.close()
        print("  Tarea configurada como vencida dentro de ventana para despacho programado.")

        # Lanzar el demonio como proceso separado con --once y ventana extendida
        print("  Lanzando proceso real: 'python scripts/run_execution_daemon.py --interval 0.5 --once --window-start 00:00 --window-end 23:59'...")
        daemon_proc = subprocess.run(
            [
                sys.executable,
                "scripts/run_execution_daemon.py",
                "--interval", "0.5",
                "--once",
                "--window-start", "00:00",
                "--window-end", "23:59",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert daemon_proc.returncode == 0, f"El demonio falló: {daemon_proc.stderr}"
        print("  Demonio ejecutado exitosamente y finalizado con código 0.")

        # Verificar persistencia del resultado en la base de datos
        con = sqlite3.connect(db_file)
        cur = con.cursor()
        cur.execute("SELECT status, attempts, locked_by FROM scheduled_tasks WHERE candidacy_id = ?", (cand_id,))
        task_row = cur.fetchone()
        assert task_row[0] == "completed", f"Estado de tarea esperado 'completed', obtenido {task_row[0]}"
        assert task_row[1] == 1, f"Intentos esperados 1, obtenidos {task_row[1]}"
        print(f"  Verificación de tarea: status='{task_row[0]}', attempts={task_row[1]}.")

        cur.execute("SELECT result, action, evidence FROM execution_logs WHERE candidacy_id = ?", (cand_id,))
        log_row = cur.fetchone()
        assert log_row[0] == "success", f"Resultado en log esperado 'success', obtenido {log_row[0]}"
        print(f"  Verificación de log: action='{log_row[1]}', result='{log_row[0]}', evidencia simulada registrada.")

        cur.execute("SELECT current_status FROM crm_entries WHERE candidacy_id = ?", (cand_id,))
        crm_row = cur.fetchone()
        assert crm_row[0] == "sent_confirmed", f"Estado CRM esperado 'sent_confirmed', obtenido {crm_row[0]}"
        print(f"  Verificación de CRM: current_status='{crm_row[0]}'.")

        # Demostración adicional de crash y reinicio seguro (Anti-duplicación P1 de dictamen a99b9f2)
        print("\n  Demostración de Crash Recovery y Reinicio tras Caída:")
        # Simular una tarea tomada por un worker que cayó durante el despacho con lease expirado
        cur.execute(
            "INSERT INTO scheduled_tasks (id, tenant_id, approval_id, candidacy_id, scheduled_date, scheduled_time, "
            "execution_timezone, execution_instant_utc, status, idempotency_key, locked_by, locked_at, lease_expires_at, "
            "fence_token, dispatch_in_flight, attempts, max_attempts, created_at, updated_at) "
            "VALUES ('task-crash-demo', ?, ?, ?, '2026-10-16', '09:00:00', 'America/Mexico_City', "
            "'2020-01-01 00:00:00', 'executing', 'idemp-crash-demo', 'crashed-worker-pid-888', '2020-01-01 00:00:00', "
            "'2020-01-01 00:01:00', 1, 1, 1, 1, '2020-01-01 00:00:00', '2020-01-01 00:00:00')",
            (tenant_id, approval_id, cand_id),
        )
        con.commit()
        con.close()

        print("  Simulada tarea en vuelo ('executing') abandonada por worker caído con lease expirado.")
        print("  Lanzando nuevo demonio independiente para demostrar recuperación conservadora...")

        daemon_restart_proc = subprocess.run(
            [sys.executable, "scripts/run_execution_daemon.py", "--interval", "0.5", "--once"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert daemon_restart_proc.returncode == 0

        con = sqlite3.connect(db_file)
        cur = con.cursor()
        cur.execute("SELECT status, fence_token FROM scheduled_tasks WHERE id = 'task-crash-demo'")
        rec_task = cur.fetchone()
        assert rec_task[0] == "result_uncertain", f"Esperado result_uncertain, obtenido {rec_task[0]}"
        assert rec_task[1] >= 2, f"Esperado fence_token >= 2, obtenido {rec_task[1]}"
        print(f"  Recuperación verificada: tarea clasificada como '{rec_task[0]}' con fence_token={rec_task[1]} (no reenviada).")

        cur.execute("SELECT current_status FROM crm_entries WHERE candidacy_id = ?", (cand_id,))
        rec_crm = cur.fetchone()
        assert rec_crm[0] == "requires_intervention", f"Esperado requires_intervention, obtenido {rec_crm[0]}"
        print(f"  CRM actualizado coherentemente a '{rec_crm[0]}'.")
        con.close()

        # 6. Reporte final de tablas en SQLite
        print("\n" + "=" * 75)
        print(" EVIDENCIA DE PERSISTENCIA EN SQLITE TEMPORAL (Conteos de registros):")
        print("=" * 75)
        con = sqlite3.connect(db_file)
        cur = con.cursor()
        tables = [
            "tenants", "users", "user_profiles", "master_cvs", "vacancies",
            "evaluations", "candidacies", "approvals", "scheduled_tasks",
            "execution_logs", "crm_entries", "crm_events"
        ]
        for tbl in tables:
            cur.execute(f"SELECT COUNT(*) FROM {tbl}")
            cnt = cur.fetchone()[0]
            print(f"  - Tabla {tbl:18s}: {cnt:3d} registros persistidos.")
        con.close()

        print("\n" + "=" * 75)
        print(" DEMOSTRACIÓN COMPLETADA CON ÉXITO")
        print(f" Todos los procesos finalizaron de manera limpia (código 0).")
        print(f" Evidencias generadas en: {demo_dir}")
        print("=" * 75)

    finally:
        # Apagar el servidor uvicorn de forma limpia
        if server_proc and server_proc.poll() is None:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_proc.kill()
        print("\nProceso de servidor web terminado de forma segura.")


if __name__ == "__main__":
    main()
