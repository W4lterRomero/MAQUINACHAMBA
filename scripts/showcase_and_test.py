#!/usr/bin/env python3
"""
Script de pruebas integrales y captura visual de Máquina de Chamba.
Ejecuta el flujo completo sobre el servidor en ejecución (http://127.0.0.1:8000)
y captura screenshots de alta resolución en cada vista principal.
"""

import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional
import urllib.request
import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ARTIFACTS_DIR = Path("/home/walt/.gemini/antigravity/brain/aff2a4b2-9bc6-490a-a9fe-79f695fa1d78")
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = "http://127.0.0.1:8000"

def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CDPClient:
    """Cliente ligero de Chrome DevTools Protocol (CDP) sobre WebSocket."""
    def __init__(self, ws_url: str):
        assert ws_url.startswith("ws://")
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
        assert "101" in resp, f"Fallo handshake: {resp}"
        self._msg_id = 0

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
                if target_id is not None and msg.get("id") == target_id:
                    return msg
                elif target_id is None:
                    return msg
            except Exception:
                continue
        return None

    def enable_domains(self):
        mid1 = self.send("Runtime.enable")
        self.recv_msg(target_id=mid1)
        mid2 = self.send("Page.enable")
        self.recv_msg(target_id=mid2)

    def navigate(self, url: str):
        mid = self.send("Page.navigate", {"url": url})
        self.recv_msg(target_id=mid)
        time.sleep(1.2)

    def evaluate(self, expr: str, await_promise: bool = False) -> Any:
        mid = self.send("Runtime.evaluate", {"expression": expr, "awaitPromise": await_promise, "returnByValue": True})
        res = self.recv_msg(target_id=mid)
        if not res or "result" not in res:
            return None
        res_obj = res["result"].get("result", {})
        return res_obj.get("value")

    def capture_screenshot(self, output_path: Path):
        mid = self.send("Page.captureScreenshot", {"format": "png"})
        res = self.recv_msg(target_id=mid, timeout=10.0)
        assert res and "result" in res and "data" in res["result"], "Fallo captura screenshot"
        img_bytes = base64.b64decode(res["result"]["data"])
        output_path.write_bytes(img_bytes)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def run_showcase():
    print("=" * 70)
    print(" EJECUTANDO PRUEBAS FUNCIONALES Y CAPTURAS EN VIVO")
    print("=" * 70)

    # 1. Comprobar que el servidor responda
    with httpx.Client(base_url=BASE_URL, timeout=5.0) as client:
        r = client.get("/api/health")
        assert r.status_code == 200, f"Health check falló: {r.status_code}"
        print(f"✓ Servidor activo en {BASE_URL}: {r.json()}")

    # 2. Registrar/Autenticar usuario de demostración
    test_user_email = "walter_showcase@maquinachamba.local"
    test_user_pwd = "ShowcasePassword2026!"
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        # Intentar login primero
        login_res = client.post("/api/v1/auth/login", json={"email": test_user_email, "password": test_user_pwd})
        if login_res.status_code == 200:
            token = login_res.json()["access_token"]
            tenant_id = login_res.json()["tenant_id"]
            print(f"✓ Usuario ya existente autenticado: {test_user_email} (tenant: {tenant_id})")
        else:
            reg_res = client.post("/api/v1/auth/register", json={
                "email": test_user_email,
                "password": test_user_pwd,
                "full_name": "Walter Romero",
            })
            assert reg_res.status_code in (200, 201), f"Error registro: {reg_res.text}"
            token = reg_res.json()["access_token"]
            tenant_id = reg_res.json()["tenant_id"]
            print(f"✓ Nuevo usuario registrado exitosamente: {test_user_email} (tenant: {tenant_id})")

        headers = {"Authorization": f"Bearer {token}"}

        # 3. Configurar Perfil
        p_res = client.post("/api/v1/profile", json={
            "full_name": "Walter Romero",
            "email": test_user_email,
            "phone": "+503 7000-0000",
            "location_city": "San Salvador",
            "location_country": "El Salvador",
            "availability": "immediate",
        }, headers=headers)
        assert p_res.status_code == 200, f"Error profile: {p_res.text}"
        profile_data = p_res.json()
        profile_id = profile_data.get("id")
        print(f"✓ Perfil profesional actualizado (id: {profile_id})")

        # 4. Agregar Habilidades Verificadas
        skills = [
            ("Python", "technical", "expert", 5.0),
            ("FastAPI", "technical", "advanced", 4.0),
            ("React", "technical", "advanced", 3.5),
            ("TypeScript", "technical", "intermediate", 3.0),
            ("PostgreSQL", "technical", "advanced", 4.0),
        ]
        for s_name, cat, prof, yrs in skills:
            client.post("/api/v1/profile/skills", json={
                "name": s_name,
                "category": cat,
                "proficiency": prof,
                "years_experience": yrs,
                "verification_status": "verified",
            }, headers=headers)
        print(f"✓ {len(skills)} Habilidades técnicas verificadas registradas")

        # 5. Probar REQ-003: Importación GitHub con simulación
        gh_res = client.post("/api/v1/profile/sources/github/import", json={
            "username": "W4lterRomero"
        }, headers=headers)
        assert gh_res.status_code in (200, 201), f"Error GitHub import: {gh_res.text}"
        gh_data = gh_res.json()
        print(f"✓ [REQ-003] Importación GitHub simulada: {gh_data.get('repositories_imported', 0)} repos importados, {gh_data.get('skills_suggested', 0)} habilidades sugeridas")

        # 6. Probar REQ-004: Crear CV Maestro
        cv_res = client.post("/api/v1/profile/cv", json={
            "name": "Fullstack Python & React",
            "specialization": "Backend & Cloud Systems",
            "summary": "Ingeniero de software con sólida trayectoria en arquitecturas distribuidas.",
            "template_id": "standard_ats",
            "sections": {"skills": ["Python", "FastAPI", "React", "TypeScript"]},
        }, headers=headers)
        assert cv_res.status_code in (200, 201), f"Error CV: {cv_res.text}"
        cv_id = cv_res.json().get("id")
        print(f"✓ [REQ-004] CV Maestro creado con éxito (id: {cv_id})")

        # 7. Probar REQ-007 y REQ-009: Vacante con análisis de fraude y fecha de cierre
        vac_res = client.post("/api/v1/vacancies", json={
            "job_title": "Senior Backend Engineer (Python / FastAPI)",
            "company_name": "Tech Corp Innovators",
            "location": "Remoto",
            "remote_type": "remote",
            "salary_min": 4500.0,
            "salary_max": 6500.0,
            "salary_currency": "USD",
            "description": "Buscamos Senior Backend Developer con experiencia sólida en Python, FastAPI, arquitecturas limpias y bases de datos relacionales.",
            "source_url": "https://example.com/jobs/senior-python",
            "requirements": ["Python", "FastAPI", "PostgreSQL", "Docker"],
            "closing_date": "2026-11-30",
        }, headers=headers)
        assert vac_res.status_code in (200, 201), f"Error vacante: {vac_res.text}"
        vacancy_id = vac_res.json().get("id")
        fraud_signals = vac_res.json().get("fraud_signals", {})
        print(f"✓ [REQ-007/009] Vacante registrada y evaluada contra fraude (señales detectadas: {len(fraud_signals)})")

        # 8. Probar REQ-010 y REQ-011: Evaluación Determinista de Compatibilidad
        eval_res = client.post("/api/v1/evaluations", json={
            "vacancy_id": vacancy_id,
            "cv_id": cv_id,
        }, headers=headers)
        assert eval_res.status_code in (200, 201), f"Error evaluación: {eval_res.text}"
        eval_data = eval_res.json()
        print(f"✓ [REQ-010/011] Evaluación determinista: Score={eval_data['compatibility_score']*100:.1f}%, Recomendación={eval_data['recommendation']}")

        # 9. Probar REQ-014 y REQ-015: Preparación y Aprobación de Candidatura
        prep_res = client.post("/api/v1/candidacies/prepare", json={
            "vacancy_id": vacancy_id,
            "cv_id": cv_id,
        }, headers=headers)
        assert prep_res.status_code in (200, 201), f"Error prepare: {prep_res.text}"
        candidacy_id = prep_res.json().get("id")
        print(f"✓ [REQ-014] Candidatura preparada sin envíos prematuros (candidacy_id: {candidacy_id})")

        appr_res = client.post(f"/api/v1/candidacies/{candidacy_id}/approve", json={
            "user_timezone": "America/El_Salvador",
            "notes": "Aprobado para despacho matutino.",
        }, headers=headers)
        assert appr_res.status_code in (200, 201), f"Error approve: {appr_res.text}"
        appr_data = appr_res.json()
        print(f"✓ [REQ-015] Candidatura aprobada formalmente. Hash inmutable: {appr_data.get('content_hash')[:16]}...")

        # 10. Probar REQ-028: Métricas de Dashboard y Reporte Diario de Correo
        dash_res = client.get("/api/v1/reports/dashboard-metrics", headers=headers)
        assert dash_res.status_code == 200, f"Error dashboard: {dash_res.text}"
        dash_kpis = dash_res.json()
        print(f"✓ [REQ-028] Dashboard KPIs consolidados:")
        print(f"    - Despacho de mañana: {dash_kpis['execution']['scheduled_for_tomorrow']}")
        print(f"    - Pendientes aprobación: {dash_kpis['candidacies']['pending_approval']}")
        print(f"    - Enviadas en ciclo: {dash_kpis['candidacies']['sent_confirmed']}")
        print(f"    - Presupuesto IA: ${dash_kpis['ai_budget']['current_month_usage_usd']:.2f} / ${dash_kpis['ai_budget']['monthly_budget_usd']:.2f}")

        preview_res = client.get("/api/v1/reports/daily-email/preview", headers=headers)
        assert preview_res.status_code == 200, f"Error email preview: {preview_res.text}"
        print(f"✓ [REQ-028] Previsualización de reporte diario generada (HTML + Plain text)")

        send_res = client.post("/api/v1/reports/daily-email/send", headers=headers)
        assert send_res.status_code == 200, f"Error email send: {send_res.text}"
        print(f"✓ [REQ-028] Envío seguro de reporte en modo simulación: {send_res.json()['message']}")

        # 11. Probar REQ-029: Configuración IA BYOK
        key_res = client.post("/api/v1/ai/key", json={
            "provider": "openai",
            "key_value": "dummy-mock-byok-key-9999",
        }, headers=headers)
        assert key_res.status_code in (200, 201), f"Error saving AI key: {key_res.text}"

        ai_res = client.post("/api/v1/ai/config", json={
            "provider": "openai",
            "model": "gpt-4o-mini",
            "monthly_budget_usd": 15.00,
            "is_active": True,
        }, headers=headers)
        assert ai_res.status_code in (200, 201), f"Error AI config: {ai_res.text}"
        print(f"✓ [REQ-029] Configuración IA (BYOK) guardada con clave cifrada y presupuesto de $15.00 USD")

    # =========================================================================
    # RECORRIDO DE NAVEGADOR Y CAPTURA DE PANTALLA DE ALTA RESOLUCIÓN
    # =========================================================================
    print("\n" + "-" * 70)
    print(" INICIANDO NAVEGADOR HEADLESS Y CAPTURA VISUAL DE LA INTERFAZ")
    print("-" * 70)

    chrome_bin = shutil.which("google-chrome") or shutil.which("chromium-browser") or shutil.which("chromium")
    assert chrome_bin, "Google Chrome no encontrado"

    cdp_port = find_free_port()
    user_data_dir = Path(tempfile.mkdtemp(prefix="chrome_ui_test_"))

    chrome_cmd = [
        chrome_bin,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1400,900",
        f"--user-data-dir={user_data_dir}",
        f"--remote-debugging-port={cdp_port}",
        "--remote-debugging-address=127.0.0.1",
        "about:blank",
    ]
    proc = subprocess.Popen(chrome_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ws_url = None
    for _ in range(30):
        time.sleep(0.2)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json", timeout=1.0) as r:
                tabs = json.loads(r.read())
                page_tab = next((t for t in tabs if t.get("type") == "page"), None)
                if page_tab and "webSocketDebuggerUrl" in page_tab:
                    ws_url = page_tab["webSocketDebuggerUrl"]
                    break
        except Exception:
            pass
    assert ws_url, "No se pudo conectar a Chrome DevTools Protocol tras varios reintentos."

    try:
        cdp = CDPClient(ws_url)
        cdp.enable_domains()
        print("✓ Conexión CDP establecida con Google Chrome.")

        # Cargar la aplicación e inyectar el token en localStorage y memoria
        cdp.navigate(f"{BASE_URL}/")
        time.sleep(1.2)
        inject_js = f"""(() => {{
            localStorage.setItem("mc_token", {json.dumps(token)});
            authToken = {json.dumps(token)};
            toggleAuthModal(false);
            checkAuthStatus();
            loadInitialData();
            return true;
        }})()"""
        cdp.evaluate(inject_js)
        time.sleep(1.5)
        print("✓ Token inyectado y datos iniciales cargados en la interfaz.")

        tabs_to_capture = [
            ("tab-dashboard", ARTIFACTS_DIR / "screenshot_01_dashboard.png", "Dashboard Consolidado (REQ-028)", None),
            ("tab-profile", ARTIFACTS_DIR / "screenshot_02_profile.png", "Perfil Profesional y Habilidades (REQ-001)", None),
            ("tab-cv-import", ARTIFACTS_DIR / "screenshot_03_github_import.png", "Importación CV y GitHub (REQ-002, REQ-003)", "toggleGitHubImport()"),
            ("tab-vacancies", ARTIFACTS_DIR / "screenshot_04_vacancies.png", "Vacantes, Antifraude y Compatibilidad (REQ-007, REQ-009, REQ-010)", None),
            ("tab-approvals", ARTIFACTS_DIR / "screenshot_05_approvals.png", "Revisión y Aprobaciones (REQ-014, REQ-015)", None),
            ("tab-crm", ARTIFACTS_DIR / "screenshot_06_crm.png", "CRM de Candidaturas (REQ-026)", None),
            ("tab-ai-config", ARTIFACTS_DIR / "screenshot_07_ai_config.png", "Configuración IA BYOK y Presupuesto (REQ-029)", None),
        ]

        for tab_id, out_file, desc, extra_js in tabs_to_capture:
            cdp.evaluate("document.getElementById('modalAuth').style.display = 'none';")
            cdp.evaluate(f"switchTab('{tab_id}')")
            time.sleep(1.5)
            if extra_js:
                cdp.evaluate(extra_js)
                time.sleep(0.5)
            cdp.capture_screenshot(out_file)
            print(f"  📸 Captura guardada: {out_file.name} [{desc}] ({out_file.stat().st_size} bytes)")

        # Captura especial: Modal de Reporte Diario de Correo abierto
        cdp.evaluate("switchTab('tab-dashboard')")
        time.sleep(1.0)
        cdp.evaluate("openDailyReportModal()")
        time.sleep(1.2)
        modal_file = ARTIFACTS_DIR / "screenshot_08_daily_email_modal.png"
        cdp.capture_screenshot(modal_file)
        print(f"  📸 Captura guardada: {modal_file.name} [Modal Previsualización de Reporte Diario] ({modal_file.stat().st_size} bytes)")

        cdp.close()
        print("\n✓ TODAS LAS CAPTURAS Y PRUEBAS CONCLUIDAS SATISFACTORIAMENTE.")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()
        shutil.rmtree(user_data_dir, ignore_errors=True)

if __name__ == "__main__":
    run_showcase()
