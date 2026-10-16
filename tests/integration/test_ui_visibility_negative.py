"""
Pruebas negativas de visibilidad en UI y detección de toast de error.
Demuestra que las aserciones de interfaz (is_visible, assert_visible, has_error_toast)
rechazan estrictamente pestañas ocultas, elementos con dimensiones 0 y detectan errores.
"""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request
import pytest

CHROME_BIN = shutil.which("google-chrome") or shutil.which("chromium")
pytestmark = pytest.mark.skipif(not CHROME_BIN, reason="Google Chrome / Chromium no disponible")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.demo_e2e_walkthrough import CDPClient, find_free_port


TEST_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { margin: 0; padding: 20px; font-family: sans-serif; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
  #toast { display: none; padding: 10px; border: 1px solid #3b82f6; }
  #toast.toast-error { border-color: #ef4444; }
  #zeroSizeElement { display: block; width: 0px; height: 0px; overflow: hidden; }
</style>
</head>
<body>
  <div id="tab-profile" class="tab-content active">Perfil activo</div>
  <div id="tab-approvals" class="tab-content">
    <div id="candidacyReviewContainer">
      <textarea id="txtCoverLetter">Carta de prueba</textarea>
      <pre id="preCvSnapshot">{"cv": "data"}</pre>
      <span id="lblContentHash">abc123hash</span>
      <button id="btnApproveCandidacy" style="width: 100px; height: 30px;">Aprobar</button>
    </div>
  </div>
  <div id="zeroSizeElement">Texto oculto por dimensiones</div>
  <div id="toast">Mensaje de notificación</div>
</body>
</html>
"""


@pytest.fixture(scope="module")
def browser_test_env(tmp_path_factory):
    """Inicia Chrome headless sobre archivo HTML local para pruebas de aserciones de visibilidad."""
    temp_dir = tmp_path_factory.mktemp("ui_negative")
    html_file = temp_dir / "test_fixture.html"
    html_file.write_text(TEST_HTML, encoding="utf-8")

    chrome_user_dir = temp_dir / "chrome_profile"
    chrome_user_dir.mkdir()

    port_cdp = find_free_port()
    chrome_proc = subprocess.Popen(
        [
            CHROME_BIN,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--window-size=1366,768",
            f"--user-data-dir={chrome_user_dir}",
            f"--remote-debugging-port={port_cdp}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Esperar conexión CDP
    ws_url = None
    for _ in range(50):
        try:
            req = urllib.request.urlopen(f"http://127.0.0.1:{port_cdp}/json", timeout=1.0)
            tabs = json.loads(req.read().decode("utf-8"))
            page_tab = next((t for t in tabs if t.get("type") == "page"), None)
            if page_tab and "webSocketDebuggerUrl" in page_tab:
                ws_url = page_tab["webSocketDebuggerUrl"]
                break
        except Exception:
            time.sleep(0.1)

    assert ws_url, "No se pudo conectar a Chrome Headless"
    cdp = CDPClient(ws_url)
    cdp.enable_domains()
    cdp.navigate(f"file://{html_file.resolve()}")
    cdp.wait_for_predicate("document.getElementById('tab-approvals') !== null", timeout=8.0)

    yield cdp

    cdp.close()
    chrome_proc.terminate()
    try:
        chrome_proc.wait(timeout=3)
    except Exception:
        chrome_proc.kill()


def test_hidden_tab_and_child_buttons_are_rejected(browser_test_env: CDPClient):
    """Demuestra que una pestaña oculta (display: none) y sus botones fallan assert_visible."""
    cdp = browser_test_env
    # Asegurar que tab-approvals no tiene la clase .active (display: none)
    cdp.evaluate("document.getElementById('tab-approvals').classList.remove('active')")
    assert cdp.is_visible("#tab-approvals") is False
    assert cdp.is_visible("#btnApproveCandidacy") is False

    with pytest.raises(AssertionError, match="NO es visible en pantalla"):
        cdp.assert_visible("#tab-approvals", "Pestaña oculta")

    with pytest.raises(AssertionError, match="NO es visible en pantalla"):
        cdp.assert_visible("#btnApproveCandidacy", "Botón en pestaña oculta")


def test_zero_dimensions_element_is_rejected(browser_test_env: CDPClient):
    """Demuestra que un elemento con width=0 o height=0 no se considera visible."""
    cdp = browser_test_env
    assert cdp.is_visible("#zeroSizeElement") is False
    with pytest.raises(AssertionError, match="NO es visible en pantalla"):
        cdp.assert_visible("#zeroSizeElement", "Elemento con dimensiones 0")


def test_visible_tab_is_accepted(browser_test_env: CDPClient):
    """Demuestra que al hacer visible la pestaña (.active), assert_visible pasa con éxito."""
    cdp = browser_test_env
    # Activar la pestaña
    cdp.evaluate("document.getElementById('tab-approvals').classList.add('active')")
    assert cdp.is_visible("#tab-approvals") is True
    assert cdp.is_visible("#btnApproveCandidacy") is True
    assert cdp.is_visible("#txtCoverLetter") is True
    assert cdp.is_visible("#preCvSnapshot") is True
    assert cdp.is_visible("#lblContentHash") is True

    # assert_visible no debe lanzar excepción
    cdp.assert_visible("#tab-approvals")
    cdp.assert_visible("#btnApproveCandidacy")


def test_error_toast_detection(browser_test_env: CDPClient):
    """Demuestra que has_error_toast detecta estados de error y no falsos positivos."""
    cdp = browser_test_env
    # Limpiar estado inicial de toast
    cdp.evaluate("""(() => {
        const t = document.getElementById('toast');
        t.style.display = 'none';
        t.innerText = '';
        t.removeAttribute('data-error');
        t.classList.remove('toast-error');
    })()""")
    assert cdp.has_error_toast() is False

    # Toast informativo regular (sin error)
    cdp.evaluate("""(() => {
        const t = document.getElementById('toast');
        t.innerText = 'Operación completada';
        t.style.display = 'block';
    })()""")
    assert cdp.has_error_toast() is False

    # Toast con clase de error
    cdp.evaluate("""(() => {
        const t = document.getElementById('toast');
        t.innerText = 'Error: Cannot read properties of undefined';
        t.classList.add('toast-error');
        t.setAttribute('data-error', 'true');
        t.style.display = 'block';
    })()""")
    assert cdp.has_error_toast() is True

    # Limpiar toast
    cdp.evaluate("document.getElementById('toast').style.display = 'none'")
    assert cdp.has_error_toast() is False
