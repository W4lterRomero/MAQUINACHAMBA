#!/usr/bin/env python3
"""
Demostración reproducible completa de Máquina de Chamba — Fase 1 (Modo Simulación Estricto).
Atiende rigurosamente el dictamen de supervisión de Work:

1. SMOKE TESTS DE API Y FIXTURES:
   Verificación basal de endpoints REST y consistencia de modelos.

2. RECORRIDO REAL MEDIANTE FORMULARIOS Y BOTONES DEL NAVEGADOR:
   Google Chrome Headless controlado por Chrome DevTools Protocol (CDP) sobre WebSocket.
   Automatización real de inputs y clicks:
   - Registro de usuario mediante formulario (#modalAuth).
   - Configuración de perfil profesional y creación de CV maestro (#frmProfile, #frmCV).
   - Importación de vacante con validación anti-SSRF (#frmVacancy).
   - Evaluación y revisión humana obligatoria (inspección de hash SHA-256 inmutable).
   - Aprobación explícita mediante botón con programación para mañana (#candidacyReviewContainer).
   - Tablero CRM con verificación visible en columna de programadas (#col-scheduled).
   - Captura de screenshots en cada hito y verificación de 0 errores en consola JS.

3. SERVICIO EN BUCLE AUTÓNOMO CON RELOJ CONTROLABLE (SIN REESCRITURA SQL):
   - Reloj de pruebas sincronizado entre procesos vía MAQUINACHAMBA_TEST_CLOCK_FILE.
   - Cero envíos antes del instante autorizado (el demonio itera en bucle y respeta la ventana).
   - Un único envío al alcanzar la hora aprobada (transición a completed, log y CRM sent_confirmed).
   - Persistencia tras reinicio sin duplicar la ejecución.

4. INTERRUPCIÓN REAL DEL PROCESO DURANTE DESPACHO CON BARRERA Y RECUPERACIÓN:
   - Adaptador con barrera de sincronización (MAQUINACHAMBA_DISPATCH_BARRIER_FILE).
   - Envío de señal SIGKILL (kill -9) mientras el despacho está en vuelo (dispatch_in_flight=True).
   - Reinicio del demonio tras expiración del lease.
   - Recuperación conservadora a result_uncertain con fencing token incrementado y CRM requires_intervention.
   - Cero reenvíos duplicados.

5. AUDITORÍA DE PERSISTENCIA EN SQLITE TEMPORAL:
   - Conteo de registros en las 12 tablas principales.
"""

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional
import urllib.request
import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent


def find_free_port() -> int:
    """Encuentra un puerto TCP local disponible."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CDPClient:
    """
    Cliente ligero de Chrome DevTools Protocol (CDP) sobre WebSocket estándar (RFC 6455).
    No requiere dependencias externas; permite automatizar clics, formularios, capturas
    y recopilar excepciones y mensajes de consola en tiempo real.
    """

    def __init__(self, ws_url: str):
        assert ws_url.startswith("ws://"), f"URL inválida para CDP: {ws_url}"
        host_port, path = ws_url[5:].split("/", 1)
        host, port = host_port.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=10.0)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        resp = self.sock.recv(4096).decode("latin1")
        assert "101" in resp, f"Fallo de handshake WebSocket: {resp}"
        self._msg_id = 0
        self.console_messages: List[Dict[str, Any]] = []
        self.exceptions: List[Dict[str, Any]] = []

    def send(self, method: str, params: Optional[Dict[str, Any]] = None) -> int:
        self._msg_id += 1
        msg_id = self._msg_id
        payload = json.dumps({"id": msg_id, "method": method, "params": params or {}}).encode("utf-8")
        mask = os.urandom(4)
        masked = bytearray(payload)
        for i in range(len(masked)):
            masked[i] ^= mask[i % 4]
        frame = bytearray([0x81])
        length = len(payload)
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame.extend(length.to_bytes(2, "big"))
        else:
            frame.append(0x80 | 127)
            frame.extend(length.to_bytes(8, "big"))
        frame.extend(mask)
        frame.extend(masked)
        self.sock.sendall(frame)
        return msg_id

    def recv_msg(self, target_id: Optional[int] = None, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        self.sock.settimeout(timeout)
        start = time.time()
        while time.time() - start < timeout:
            try:
                hdr = self.sock.recv(2)
            except socket.timeout:
                break
            if not hdr:
                break
            b1, b2 = hdr[0], hdr[1]
            is_masked = bool(b2 & 0x80)
            pay_len = b2 & 0x7F
            if pay_len == 126:
                pay_len = int.from_bytes(self.sock.recv(2), "big")
            elif pay_len == 127:
                pay_len = int.from_bytes(self.sock.recv(8), "big")
            mask = self.sock.recv(4) if is_masked else None
            data = bytearray()
            while len(data) < pay_len:
                chunk = self.sock.recv(min(pay_len - len(data), 65536))
                if not chunk:
                    break
                data.extend(chunk)
            if is_masked and mask:
                for i in range(len(data)):
                    data[i] ^= mask[i % 4]
            try:
                msg = json.loads(data.decode("utf-8"))
            except Exception:
                continue

            # Registrar eventos de consola y excepciones en tiempo real
            method = msg.get("method")
            if method == "Runtime.consoleAPICalled":
                self.console_messages.append(msg["params"])
            elif method == "Runtime.exceptionThrown":
                self.exceptions.append(msg["params"])
            elif target_id is not None and msg.get("id") == target_id:
                return msg
            elif target_id is None:
                return msg
        return None

    def enable_domains(self):
        mid1 = self.send("Runtime.enable")
        self.recv_msg(target_id=mid1)
        mid2 = self.send("Page.enable")
        self.recv_msg(target_id=mid2)

    def navigate(self, url: str):
        mid = self.send("Page.navigate", {"url": url})
        self.recv_msg(target_id=mid)
        # Esperar brevemente a que el DOM inicial cargue
        time.sleep(1.0)

    def evaluate(self, expr: str, await_promise: bool = False) -> Any:
        mid = self.send("Runtime.evaluate", {"expression": expr, "awaitPromise": await_promise, "returnByValue": True})
        res = self.recv_msg(target_id=mid)
        if not res or "result" not in res:
            return None
        res_obj = res["result"].get("result", {})
        return res_obj.get("value")

    def click(self, selector: str):
        js = f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return false;
            el.click();
            return true;
        }})()"""
        ok = self.evaluate(js)
        assert ok is True, f"Elemento no encontrado o no clickeable: {selector}"

    def fill(self, selector: str, value: str):
        js = f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return false;
            el.value = {json.dumps(value)};
            el.dispatchEvent(new Event("input", {{ bubbles: true }}));
            el.dispatchEvent(new Event("change", {{ bubbles: true }}));
            return true;
        }})()"""
        ok = self.evaluate(js)
        assert ok is True, f"Elemento no encontrado para rellenar: {selector}"

    def submit(self, form_selector: str):
        js = f"""(() => {{
            const el = document.querySelector({json.dumps(form_selector)});
            if (!el) return false;
            el.dispatchEvent(new Event("submit", {{ bubbles: true, cancelable: true }}));
            return true;
        }})()"""
        ok = self.evaluate(js)
        assert ok is True, f"Formulario no encontrado para submit: {form_selector}"

    def wait_for_predicate(self, predicate_expr: str, timeout: float = 10.0) -> Any:
        start = time.time()
        while time.time() - start < timeout:
            res = self.evaluate(predicate_expr)
            if res:
                return res
            time.sleep(0.15)
        raise TimeoutError(f"Tiempo agotado esperando predicado JS: {predicate_expr}")

    def capture_screenshot(self, output_path: Path):
        mid = self.send("Page.captureScreenshot", {"format": "png"})
        res = self.recv_msg(target_id=mid, timeout=10.0)
        assert res and "result" in res and "data" in res["result"], "Fallo al capturar screenshot vía CDP"
        img_bytes = base64.b64decode(res["result"]["data"])
        output_path.write_bytes(img_bytes)

    def get_dom_html(self) -> str:
        html = self.evaluate("document.documentElement.outerHTML")
        return str(html or "")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def main():
    print("=" * 80)
    print(" DEMOSTRACIÓN E2E EXHAUSTIVA: INTERFAZ, SERVICIO AUTÓNOMO Y RECUPERACIÓN")
    print(" Máquina de Chamba — Fase 1 (Modo Simulación Estricto)")
    print("=" * 80)

    # 1. Configuración de entorno temporal aislado
    demo_dir = Path(tempfile.mkdtemp(prefix="maquinachamba_e2e_"))
    db_file = demo_dir / "e2e_maquinachamba.db"
    secret_file = demo_dir / ".secret_key"
    clock_file = demo_dir / "simulated_clock.txt"
    barrier_file = demo_dir / "dispatch.barrier"
    screenshots_dir = demo_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    # Fijar reloj inicial en jueves 15 de Octubre de 2026, 10:00 UTC (04:00 hora local Ciudad de México)
    initial_clock = datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone.utc)
    clock_file.write_text(initial_clock.isoformat(), encoding="utf-8")

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    print(f"\n[1/5] Configuración del entorno temporal:")
    print(f"  - Directorio temporal: {demo_dir}")
    print(f"  - Base de datos SQLite: {db_file}")
    print(f"  - Archivo de clave protegida: {secret_file}")
    print(f"  - Archivo de reloj controlable: {clock_file}")
    print(f"  - Puerto API local: {port}")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file}"
    env["APP_SECRET_PATH"] = str(secret_file)
    env["APP_PORT"] = str(port)
    env["APP_HOST"] = "127.0.0.1"
    env["SIMULATION_MODE"] = "true"
    env["APP_ENV"] = "development"
    env["MAQUINACHAMBA_TEST_CLOCK_FILE"] = str(clock_file)

    server_proc = None
    chrome_proc = None

    try:
        # 2. Inicializar base de datos
        print("\n[2/5] Inicializando esquema de base de datos en SQLite temporal...")
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

        # 3. Iniciar servidor web uvicorn en subproceso independiente
        print(f"\n[3/5] Iniciando servidor web uvicorn (PID separado)...")
        server_log = demo_dir / "uvicorn.log"
        with open(server_log, "w", encoding="utf-8") as log_out:
            server_proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                env=env,
                stdout=log_out,
                stderr=subprocess.STDOUT,
            )

        # Esperar a que /api/health responda
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
        assert ready, "El servidor uvicorn no respondió a tiempo."
        print(f"  Servidor respondiendo OK en {base_url}/api/health.")

        # =========================================================================
        # PARTE 1: SMOKE TESTS DE API (Línea base de fixtures y endpoints REST)
        # =========================================================================
        print("\n" + "-" * 75)
        print(" PARTE 1: SMOKE TESTS DE API Y ENDPOINTS REST")
        print("-" * 75)
        with httpx.Client(base_url=base_url, timeout=10.0) as client:
            smoke_reg = client.post("/api/v1/auth/register", json={
                "email": "smoke_tester@maquinachamba.local",
                "password": "PasswordSmoke2026!",
                "full_name": "Tester Smoke API",
            })
            assert smoke_reg.status_code == 201, f"Smoke reg falló: {smoke_reg.text}"
            smoke_token = smoke_reg.json()["access_token"]
            smoke_tenant = smoke_reg.json()["tenant_id"]
            smoke_headers = {"Authorization": f"Bearer {smoke_token}"}
            print(f"  [Smoke API] Registro exitoso con asignación server-side: tenant={smoke_tenant}")

            smoke_prof = client.post("/api/v1/profile", json={
                "full_name": "Tester Smoke API",
                "email": "smoke_tester@maquinachamba.local",
                "location_city": "CDMX",
                "location_country": "México",
                "availability": "immediate",
            }, headers=smoke_headers)
            assert smoke_prof.status_code == 200
            print("  [Smoke API] Perfil validado vía API.")

        # =========================================================================
        # PARTE 2: RECORRIDO REAL MEDIANTE FORMULARIOS Y BOTONES DEL NAVEGADOR
        # =========================================================================
        print("\n" + "-" * 75)
        print(" PARTE 2: RECORRIDO REAL DE NAVEGADOR (FORMULARIOS, BOTONES, DOM Y CONSOLA)")
        print("-" * 75)

        # Regla estricta de Work: El fallo o ausencia de navegador DEBE fallar esa prueba, no aprobarla
        chrome_bin = shutil.which("google-chrome") or shutil.which("chromium-browser") or shutil.which("chromium")
        if not chrome_bin:
            raise FileNotFoundError(
                "FALLO CRÍTICO: Google Chrome o Chromium no está instalado en el sistema. "
                "Conforme al dictamen de Work, la ausencia de navegador falla la prueba."
            )
        print(f"  Ejecutable de navegador detectado: {chrome_bin}")

        cdp_port = find_free_port()
        chrome_user_dir = demo_dir / "chrome_user_data"
        chrome_user_dir.mkdir(parents=True, exist_ok=True)

        chrome_cmd = [
            chrome_bin,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--window-size=1366,768",
            f"--user-data-dir={chrome_user_dir}",
            f"--remote-debugging-port={cdp_port}",
            "about:blank",
        ]
        chrome_proc = subprocess.Popen(chrome_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)

        # Obtener la URL del WebSocket del tab principal vía endpoint JSON de Chrome
        with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json") as r:
            tabs = json.loads(r.read())
            page_tab = next(t for t in tabs if t.get("type") == "page")
            ws_url = page_tab["webSocketDebuggerUrl"]

        cdp = CDPClient(ws_url)
        cdp.enable_domains()
        print("  Conexión a Chrome DevTools Protocol (CDP) establecida exitosamente.")

        # Navegar a la aplicación
        app_url = f"{base_url}/app"
        print(f"  Navegando a {app_url}...")
        cdp.navigate(app_url)

        # Paso 2.1: Registro de usuario desde la interfaz
        print("\n  [UI 1/6] Registrando nuevo usuario mediante formulario modal...")
        cdp.click("#btnAuthToggle")
        time.sleep(0.3)
        cdp.click("#lnkToggleAuthMode")
        time.sleep(0.3)
        cdp.fill("#authFullName", "Candidata Demostración UI")
        cdp.fill("#authEmail", "candidata_ui@maquinachamba.local")
        cdp.fill("#authPassword", "PasswordUI2026!")
        cdp.click("#btnAuthSubmit")

        # Verificar aserción visible en la interfaz
        cdp.wait_for_predicate("document.getElementById('lblTenant').innerText.startsWith('tenant-')", timeout=10.0)
        lbl_tenant = cdp.evaluate("document.getElementById('lblTenant').innerText")
        lbl_user = cdp.evaluate("document.getElementById('lblUser').innerText")
        print(f"  Verificación visible: Usuario='{lbl_user}', Tenant='{lbl_tenant}'.")
        scr1 = screenshots_dir / "browser_01_registered.png"
        cdp.capture_screenshot(scr1)
        print(f"  Screenshot guardado: {scr1.name} ({scr1.stat().st_size} bytes)")

        # Paso 2.2: Configuración de perfil y CV Maestro desde la interfaz
        print("\n  [UI 2/6] Configurando perfil profesional y creando CV Maestro en pestaña Perfil...")
        cdp.fill("#profName", "Candidata Demostración UI")
        cdp.fill("#profEmail", "candidata_ui@maquinachamba.local")
        cdp.fill("#profPhone", "+52 55 9876 5432")
        cdp.fill("#profCity", "Ciudad de México")
        cdp.fill("#profCountry", "México")
        cdp.submit("#frmProfile")
        time.sleep(0.5)

        cdp.fill("#cvName", "CV Arquitectura Backend")
        cdp.fill("#cvSpecialization", "Python, FastAPI, SQLite y Sistemas Distribuidos")
        cdp.submit("#frmCV")

        # Verificar aserción visible de CV en el DOM
        cdp.wait_for_predicate("document.getElementById('cvsList').innerText.includes('CV Arquitectura Backend')", timeout=8.0)
        print("  Verificación visible: CV Maestro creado y mostrado en la lista de CVs.")
        scr2 = screenshots_dir / "browser_02_profile_cv.png"
        cdp.capture_screenshot(scr2)
        print(f"  Screenshot guardado: {scr2.name} ({scr2.stat().st_size} bytes)")

        # Paso 2.3: Importación de vacante desde la interfaz
        print("\n  [UI 3/6] Conmutando a pestaña Vacantes e importando nueva vacante...")
        cdp.click("nav button:nth-child(2)")
        time.sleep(0.5)
        cdp.fill("#vacCompany", "Tech Corp Innovations")
        cdp.fill("#vacTitle", "Senior Backend Architect")
        cdp.fill("#vacUrl", "https://techcorp.example/careers/job-7741")
        cdp.fill("#vacReqs", "Python, FastAPI, SQLite, Resiliencia")
        cdp.fill("#vacDesc", "Arquitecto para sistema de búsqueda laboral local-first.")
        cdp.submit("#frmVacancy")

        # Verificar aserción visible en la tabla
        cdp.wait_for_predicate("document.getElementById('tblVacancies').innerText.includes('Senior Backend Architect')", timeout=8.0)
        print("  Verificación visible: Vacante mostrada en la tabla de vacantes.")
        scr3 = screenshots_dir / "browser_03_vacancies.png"
        cdp.capture_screenshot(scr3)
        print(f"  Screenshot guardado: {scr3.name} ({scr3.stat().st_size} bytes)")

        # Paso 2.4: Evaluación y Revisión Humana Obligatoria desde la interfaz
        print("\n  [UI 4/6] Clic en 'Evaluar y Revisar' -> Generación e inspección humana del paquete...")
        # Clic en el botón "Evaluar y Revisar" de la primera fila
        cdp.click("#tblVacancies tbody tr:first-child button")

        # Esperar a que la pestaña de Aprobaciones se active y cargue el contenedor de revisión
        cdp.wait_for_predicate(
            "document.getElementById('candidacyReviewContainer').innerText.includes('Candidatura #') && "
            "document.getElementById('candidacyReviewContainer').innerText.includes('Hash Inmutable')",
            timeout=10.0,
        )
        content_hash_ui = cdp.evaluate("document.querySelector('#candidacyReviewContainer .hash-tag').innerText")
        print(f"  Verificación visible: Paquete en revisión con Hash SHA-256 inmutable: {content_hash_ui}")
        scr4 = screenshots_dir / "browser_04_review_pending.png"
        cdp.capture_screenshot(scr4)
        print(f"  Screenshot guardado: {scr4.name} ({scr4.stat().st_size} bytes)")

        # Paso 2.5: Aprobación explícita mediante botón
        print("\n  [UI 5/6] Aprobación humana mediante botón 'Aprobar para Mañana 🚀'...")
        cdp.click("#candidacyReviewContainer .btn-success")
        time.sleep(1.0)
        scr5 = screenshots_dir / "browser_05_approved.png"
        cdp.capture_screenshot(scr5)
        print(f"  Screenshot guardado: {scr5.name} ({scr5.stat().st_size} bytes)")

        # Paso 2.6: Consulta del CRM Kanban desde la interfaz
        print("\n  [UI 6/6] Conmutando a pestaña CRM y verificando tarjeta en columna 'Programadas'...")
        cdp.click("nav button:nth-child(4)")
        cdp.wait_for_predicate("document.getElementById('cnt-scheduled').innerText === '1'", timeout=8.0)
        cnt_sched = cdp.evaluate("document.getElementById('cnt-scheduled').innerText")
        card_title = cdp.evaluate("document.querySelector('#col-scheduled .kanban-card h3').innerText")
        assert cnt_sched == "1", f"Esperado cnt-scheduled=1, obtenido {cnt_sched}"
        assert card_title == "Senior Backend Architect", f"Título inesperado en tarjeta CRM: {card_title}"
        print(f"  Verificación visible en CRM: 1 tarjeta en columna Programadas ({card_title}).")
        scr6 = screenshots_dir / "browser_06_crm_scheduled.png"
        cdp.capture_screenshot(scr6)
        print(f"  Screenshot guardado: {scr6.name} ({scr6.stat().st_size} bytes)")

        # Verificación de errores de consola y excepciones JavaScript
        print("\n  [Consola Navegador] Verificando ausencia total de errores JS...")
        console_errors = [m for m in cdp.console_messages if m.get("type") in ("error", "assert")]
        js_exceptions = cdp.exceptions
        if console_errors:
            print(f"  [ERROR CONSOLA]: {console_errors}")
        if js_exceptions:
            print(f"  [EXCEPCIÓN JS]: {js_exceptions}")
        assert len(console_errors) == 0, f"Se detectaron errores en la consola del navegador: {console_errors}"
        assert len(js_exceptions) == 0, f"Se detectaron excepciones no controladas en el navegador: {js_exceptions}"
        print("  Verificado: 0 errores de consola y 0 excepciones no controladas.")

        dom_final_path = demo_dir / "browser_dom_final.html"
        dom_final_path.write_text(cdp.get_dom_html(), encoding="utf-8")
        print(f"  DOM HTML extraído y guardado: {dom_final_path.name} ({dom_final_path.stat().st_size} bytes)")

        cdp.close()
        chrome_proc.terminate()
        chrome_proc.wait()
        chrome_proc = None

        # =========================================================================
        # PARTE 3: SERVICIO EN BUCLE AUTÓNOMO CON RELOJ CONTROLABLE (SIN REESCRIBIR SQL)
        # =========================================================================
        print("\n" + "-" * 75)
        print(" PARTE 3: SERVICIO EN BUCLE REAL CON RELOJ CONTROLABLE")
        print(" (Cero envíos antes del instante autorizado, un envío al alcanzarlo, sin SQL updates)")
        print("-" * 75)

        # Consultar la tarea aprobada directamente en SQLite
        con = sqlite3.connect(db_file)
        cur = con.cursor()
        cur.execute("SELECT id, scheduled_date, scheduled_time, execution_instant_utc, status, attempts FROM scheduled_tasks")
        row = cur.fetchone()
        task_id, sched_date, sched_time, inst_utc_str, status_init, attempts_init = row
        con.close()

        print(f"  Tarea creada legítimamente por aprobación:")
        print(f"    - ID: {task_id}")
        print(f"    - Fecha programada: {sched_date}")
        print(f"    - Hora inicio ventana: {sched_time}")
        print(f"    - Instante UTC calculado: {inst_utc_str}")
        print(f"    - Estado actual: {status_init}, Intentos: {attempts_init}")

        # Iniciar el demonio en modo bucle autónomo continuo (subproceso del SO)
        # NO usamos --once. El demonio sondea cada 0.2 segundos.
        print("\n  Iniciando proceso autónomo: 'python scripts/run_execution_daemon.py --interval 0.2'...")
        daemon_log = demo_dir / "daemon_loop.log"
        daemon_proc = subprocess.Popen(
            [sys.executable, "scripts/run_execution_daemon.py", "--interval", "0.2"],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=open(daemon_log, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        print(f"  Demonio iniciado con PID {daemon_proc.pid}. Ejecutándose continuamente...")

        # 3.1 Comprobación de Cero Efectos Antes del Instante Autorizado
        print("\n  [Paso 3.1] Comprobando CERO efectos antes del instante autorizado...")
        # Avanzar el reloj a las 18:00 UTC del día de aprobación (antes de mañana)
        sim_dt1 = datetime(2026, 10, 15, 18, 0, 0, tzinfo=timezone.utc)
        clock_file.write_text(sim_dt1.isoformat(), encoding="utf-8")
        time.sleep(1.0)  # Dejar que el demonio itere varios ciclos

        # Avanzar el reloj a las 12:00 UTC del día siguiente (06:00 hora local, ANTES de la ventana de 09:00)
        sim_dt2 = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
        clock_file.write_text(sim_dt2.isoformat(), encoding="utf-8")
        time.sleep(1.0)  # Dejar que el demonio itere varios ciclos más

        con = sqlite3.connect(db_file)
        cur = con.cursor()
        cur.execute("SELECT status, attempts FROM scheduled_tasks WHERE id = ?", (task_id,))
        t_row = cur.fetchone()
        cur.execute("SELECT count(*) FROM execution_logs WHERE task_id = ?", (task_id,))
        log_cnt = cur.fetchone()[0]
        cur.execute("SELECT current_status FROM crm_entries WHERE candidacy_id = (SELECT candidacy_id FROM scheduled_tasks WHERE id = ?)", (task_id,))
        crm_st = cur.fetchone()[0]
        con.close()

        assert t_row[0] == "pending", f"Esperado 'pending', obtenido '{t_row[0]}'"
        assert t_row[1] == 0, f"Esperado 0 intentos, obtenido {t_row[1]}"
        assert log_cnt == 0, f"Esperado 0 logs de ejecución, obtenido {log_cnt}"
        assert crm_st == "scheduled", f"Esperado CRM 'scheduled', obtenido '{crm_st}'"
        print(f"  Verificación exitosa: status='{t_row[0]}', attempts={t_row[1]}, logs={log_cnt}, crm='{crm_st}'.")
        print("  >> CERO EFECTOS ANTES DE LA HORA AUTORIZADA.")

        # 3.2 Comprobación de Un Único Efecto al Alcanzar el Instante Autorizado
        print("\n  [Paso 3.2] Avanzando reloj al instante autorizado (2026-10-16 15:05 UTC / 09:05 local)...")
        sim_dt3 = datetime(2026, 10, 16, 15, 5, 0, tzinfo=timezone.utc)
        clock_file.write_text(sim_dt3.isoformat(), encoding="utf-8")

        # Esperar a que el demonio en su bucle procese la tarea
        processed = False
        for _ in range(40):
            time.sleep(0.2)
            con = sqlite3.connect(db_file)
            cur = con.cursor()
            cur.execute("SELECT status, attempts FROM scheduled_tasks WHERE id = ?", (task_id,))
            st, att = cur.fetchone()
            con.close()
            if st == "completed":
                processed = True
                break

        assert processed, "El demonio en bucle no procesó la tarea tras alcanzar el instante autorizado."
        con = sqlite3.connect(db_file)
        cur = con.cursor()
        cur.execute("SELECT status, attempts FROM scheduled_tasks WHERE id = ?", (task_id,))
        t_after = cur.fetchone()
        cur.execute("SELECT action, result, evidence FROM execution_logs WHERE task_id = ?", (task_id,))
        log_after = cur.fetchone()
        cur.execute("SELECT current_status FROM crm_entries WHERE candidacy_id = (SELECT candidacy_id FROM scheduled_tasks WHERE id = ?)", (task_id,))
        crm_after = cur.fetchone()[0]
        con.close()

        assert t_after[0] == "completed", f"Esperado 'completed', obtenido {t_after[0]}"
        assert t_after[1] == 1, f"Esperado attempts=1, obtenido {t_after[1]}"
        assert log_after[0] == "submit_application" and log_after[1] == "success"
        assert crm_after == "sent_confirmed"
        print(f"  Verificación exitosa: status='{t_after[0]}', attempts={t_after[1]}, log={log_after[1]}, crm='{crm_after}'.")
        print("  >> EXACTAMENTE UN EFECTO AL ALCANZAR EL INSTANTE AUTORIZADO.")

        # 3.3 Demostración de Persistencia y Reinicio sin Doble Ejecución
        print("\n  [Paso 3.3] Deteniendo demonio y reiniciando para comprobar persistencia y cero reejecuciones...")
        daemon_proc.terminate()
        daemon_proc.wait(timeout=5)

        # Iniciar nuevo demonio con el mismo reloj
        daemon_restart_log = demo_dir / "daemon_restarted.log"
        daemon_restart = subprocess.Popen(
            [sys.executable, "scripts/run_execution_daemon.py", "--interval", "0.2"],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=open(daemon_restart_log, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        time.sleep(1.0)  # Dejar que itere en bucle
        daemon_restart.terminate()
        daemon_restart.wait(timeout=5)

        con = sqlite3.connect(db_file)
        cur = con.cursor()
        cur.execute("SELECT status, attempts FROM scheduled_tasks WHERE id = ?", (task_id,))
        t_recheck = cur.fetchone()
        cur.execute("SELECT count(*) FROM execution_logs WHERE task_id = ?", (task_id,))
        logs_recheck = cur.fetchone()[0]
        con.close()

        assert t_recheck[0] == "completed" and t_recheck[1] == 1
        assert logs_recheck == 1, f"Se duplicó el log de ejecución tras reinicio: count={logs_recheck}"
        print("  Verificado: El estado se preservó tras el reinicio; la tarea no se ejecutó dos veces.")

        # =========================================================================
        # PARTE 4: INTERRUPCIÓN REAL DURANTE DESPACHO CON BARRERA Y RECUPERACIÓN
        # =========================================================================
        print("\n" + "-" * 75)
        print(" PARTE 4: INTERRUPCIÓN REAL DEL PROCESO DURANTE DESPACHO (SIGKILL) Y RECUPERACIÓN")
        print("-" * 75)

        # Crear una segunda candidatura legítima aprobada para mañana a las 09:00
        with httpx.Client(base_url=base_url, timeout=10.0) as client:
            headers = {"Authorization": f"Bearer {smoke_token}"}
            # Vacante 2
            v2 = client.post("/api/v1/vacancies", json={
                "company_name": "Crash Recovery Corp",
                "job_title": "DevOps Resiliency Lead",
                "source_url": "https://crashcorp.example/jobs/1",
                "requirements": ["Python", "Linux", "Crash Recovery"],
            }, headers=headers).json()
            # CV 2
            cv2 = client.post("/api/v1/profile/cv", json={
                "name": "CV Resiliencia",
                "specialization": "Crash Recovery & Distributed Systems",
            }, headers=headers).json()
            # Eval 2
            ev2 = client.post("/api/v1/evaluations", json={"vacancy_id": v2["id"], "cv_id": cv2["id"]}, headers=headers).json()
            # Prep 2
            cand2 = client.post("/api/v1/candidacies/prepare", json={
                "vacancy_id": v2["id"], "cv_id": cv2["id"], "evaluation_id": ev2["id"]
            }, headers=headers).json()
            # Approve 2
            app2 = client.post(f"/api/v1/candidacies/{cand2['id']}/approve", json={
                "notes": "Aprobado para prueba de caída abrupta con barrera"
            }, headers=headers).json()

        con = sqlite3.connect(db_file)
        cur = con.cursor()
        cur.execute("SELECT id, scheduled_date, scheduled_time, execution_instant_utc FROM scheduled_tasks WHERE candidacy_id = ?", (cand2["id"],))
        task_crash_id, c_date, c_time, c_inst_str = cur.fetchone()
        con.close()
        print(f"  Segunda tarea de prueba aprobada: ID={task_crash_id}, fecha={c_date}, instant={c_inst_str}")

        # Configurar la barrera en el entorno del demonio
        if barrier_file.exists():
            barrier_file.unlink()
        env_barrier = env.copy()
        env_barrier["MAQUINACHAMBA_DISPATCH_BARRIER_FILE"] = str(barrier_file)

        # Avanzar el reloj a 1 minuto después del instante UTC calculado para que esté dentro de ventana
        c_inst_dt = datetime.fromisoformat(c_inst_str)
        if c_inst_dt.tzinfo is None:
            c_inst_dt = c_inst_dt.replace(tzinfo=timezone.utc)
        sim_dt_crash = c_inst_dt + timedelta(minutes=1)
        clock_file.write_text(sim_dt_crash.isoformat(), encoding="utf-8")
        print(f"  Reloj sincronizado al instante autorizado de la tarea: {sim_dt_crash.isoformat()}")

        print("  Lanzando demonio con barrera de despacho activada...")
        barrier_daemon = subprocess.Popen(
            [sys.executable, "scripts/run_execution_daemon.py", "--interval", "0.1"],
            cwd=str(REPO_ROOT),
            env=env_barrier,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Esperar a que el worker entre en submit_application y cree el archivo de barrera
        print("  Esperando a que el worker entre en despacho en vuelo (submit_application)...")
        barrier_hit = False
        for _ in range(100):
            if barrier_file.exists():
                barrier_hit = True
                break
            time.sleep(0.1)

        assert barrier_hit, "El worker no alcanzó la barrera de despacho a tiempo."
        print(f"  >> BARRERA ALCANZADA: Despacho en vuelo confirmado ({barrier_file.read_text()}).")

        # Comprobar en SQLite que la tarea está en estado 'executing' con dispatch_in_flight=1
        con = sqlite3.connect(db_file)
        cur = con.cursor()
        cur.execute("SELECT status, dispatch_in_flight, attempts, fence_token FROM scheduled_tasks WHERE id = ?", (task_crash_id,))
        st_crash, in_flight, att_crash, fence_crash = cur.fetchone()
        con.close()
        assert st_crash == "executing" and in_flight == 1
        print(f"  Estado en SQLite en vuelo verificado: status='{st_crash}', dispatch_in_flight={in_flight}, fence_token={fence_crash}.")

        # INTERRUPCIÓN REAL DEL PROCESO: SIGKILL inmediato
        print(f"  >> ENVIANDO SIGKILL (kill -9) AL PID {barrier_daemon.pid} (CAÍDA ABRUPTA REAL DEL PROCESO)...")
        os.kill(barrier_daemon.pid, signal.SIGKILL)
        barrier_daemon.wait()
        print("  Proceso terminado de forma forzada.")

        # Limpiar la barrera
        if barrier_file.exists():
            barrier_file.unlink()

        # Avanzar el reloj 45 segundos para que el lease_expires_at expire
        sim_dt_expired = sim_dt_crash + timedelta(seconds=45)
        clock_file.write_text(sim_dt_expired.isoformat(), encoding="utf-8")
        print("  Reloj avanzado 45s: el lease del worker caído ha expirado.")

        # Lanzar un NUEVO proceso de demonio para demostrar recuperación conservadora
        print("  Lanzando nuevo demonio independiente para ejecutar ciclo de recuperación...")
        recovery_proc = subprocess.run(
            [sys.executable, "scripts/run_execution_daemon.py", "--interval", "0.2", "--once"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert recovery_proc.returncode == 0, f"Fallo en demonio de recuperación: {recovery_proc.stderr}"

        # Verificar en SQLite la recuperación conservadora
        con = sqlite3.connect(db_file)
        cur = con.cursor()
        cur.execute("SELECT status, fence_token, attempts FROM scheduled_tasks WHERE id = ?", (task_crash_id,))
        rec_st, rec_fence, rec_att = cur.fetchone()
        cur.execute("SELECT current_status FROM crm_entries WHERE candidacy_id = ?", (cand2["id"],))
        rec_crm_st = cur.fetchone()[0]
        con.close()

        assert rec_st == "result_uncertain", f"Esperado status='result_uncertain', obtenido '{rec_st}'"
        assert rec_fence > fence_crash, f"Fencing token no se incrementó: {rec_fence} <= {fence_crash}"
        assert rec_crm_st == "requires_intervention", f"CRM esperado 'requires_intervention', obtenido '{rec_crm_st}'"

        print(f"  Verificación de recuperación conservadora tras crash:")
        print(f"    - Estado de tarea: '{rec_st}' (NO reenviada a ciegas).")
        print(f"    - Fencing token: {rec_fence} (el worker anterior queda permanentemente descartado).")
        print(f"    - Estado en CRM: '{rec_crm_st}' (solicita intervención humana).")
        print("  >> RECUPERACIÓN EXITOSA: CERO DUPLICADOS Y AISLAMIENTO DE PROCESO CAÍDO.")

        # =========================================================================
        # PARTE 5: AUDITORÍA DE PERSISTENCIA EN SQLITE TEMPORAL
        # =========================================================================
        print("\n" + "=" * 80)
        print(" PARTE 5: EVIDENCIA DE PERSISTENCIA EN SQLITE TEMPORAL (Conteos de registros):")
        print("=" * 80)
        con = sqlite3.connect(db_file)
        cur = con.cursor()
        tables = [
            "tenants", "users", "user_profiles", "master_cvs", "vacancies",
            "evaluations", "candidacies", "approvals", "scheduled_tasks",
            "execution_logs", "crm_entries", "crm_events"
        ]
        for tbl in tables:
            try:
                cur.execute(f"SELECT count(*) FROM {tbl}")
                cnt = cur.fetchone()[0]
                print(f"  - Tabla {tbl:<18}: {cnt:>3} registros persistidos.")
            except Exception as e:
                print(f"  - Tabla {tbl:<18}: ERROR ({e})")
        con.close()

        print("\n" + "=" * 80)
        print(" DEMOSTRACIÓN E2E COMPLETADA CON ÉXITO")
        print(f" Evidencias visuales (screenshots y DOM) guardadas en:\n   {screenshots_dir}")
        print("=" * 80)

    finally:
        # Limpieza limpia y segura de procesos
        if chrome_proc and chrome_proc.poll() is None:
            chrome_proc.terminate()
            try:
                chrome_proc.wait(timeout=3)
            except Exception:
                chrome_proc.kill()
        if server_proc and server_proc.poll() is None:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=3)
            except Exception:
                server_proc.kill()
            print("\nProcesos cerrados de forma limpia.")


if __name__ == "__main__":
    main()
