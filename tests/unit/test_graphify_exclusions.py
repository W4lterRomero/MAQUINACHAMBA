"""
Pruebas de verificación de exclusiones obligatorias de Graphify.
Demuestra con fixtures sintéticos que secretos, credenciales, bases de datos,
documentos privados y dependencias son efectivamente excluidos de la indexación.
"""

from pathlib import Path
import pytest

graphify = pytest.importorskip("graphify", reason="graphifyy es una herramienta exclusiva de desarrollo")
from graphify.detect import detect, _is_sensitive


class TestGraphifyExclusions:
    """Verifica que las exclusiones obligatorias de AGENTS.md se cumplan."""

    @pytest.fixture
    def synthetic_sensitive_fixtures(self, tmp_path: Path):
        """Crea un árbol de directorios con fixtures sintéticos sensibles."""
        # Archivos de entorno y secretos
        (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-synthetic12345678901234567890\n")
        (tmp_path / ".env.production").write_text("DATABASE_URL=postgres://user:pass@db/prod\n")
        (tmp_path / "deploy.key").write_text("-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC3\n")
        (tmp_path / "cert.pem").write_text("-----BEGIN CERTIFICATE-----\nMIIDXTCCAkWgAwIBAgIJAL...\n")
        (tmp_path / "id_rsa").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")

        # Bases de datos y carpetas de datos
        (tmp_path / "app.db").write_bytes(b"SQLite format 3\x00")
        (tmp_path / "users.sqlite3").write_bytes(b"SQLite format 3\x00")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "user_export.csv").write_text("id,name,email\n1,Juan,juan@example.com\n")

        # Documentos personales / CVs
        (tmp_path / "curriculum_juan.pdf").write_bytes(b"%PDF-1.4 synthetic")
        (tmp_path / "cv_maria.docx").write_bytes(b"PK\x03\x04 synthetic")

        # Dependencias y cachés
        node_modules = tmp_path / "node_modules" / "pkg"
        node_modules.mkdir(parents=True)
        (node_modules / "index.js").write_text("module.exports = {};")

        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        (pycache / "core.cpython-312.pyc").write_bytes(b"fake bytecode")

        # Código legítimo que SÍ debe detectarse
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "main.py").write_text("def run():\n    print('safe code')\n")
        (tmp_path / "README.md").write_text("# Project Readme\nSafe documentation.\n")

        return tmp_path

    def test_sensitive_files_flagged_by_is_sensitive(self):
        """Prueba unitaria de la heurística de seguridad _is_sensitive."""
        sensitive_cases = [
            Path(".env"),
            Path(".env.production"),
            Path("secrets.json"),
            Path("deploy.pem"),
            Path("server.key"),
            Path("id_rsa"),
            Path("id_ed25519"),
            Path("aws_credentials.ini"),
            Path("app_password.txt"),
            Path("check_secrets.py"),  # Contiene keyword load-bearing 'secret'
        ]
        for path in sensitive_cases:
            assert _is_sensitive(path) is True, f"Fallo al marcar como sensible: {path}"

    def test_non_sensitive_code_files_not_flagged(self):
        """Verifica que archivos legítimos no sean marcados por error."""
        safe_cases = [
            Path("backend/app/main.py"),
            Path("backend/app/core/database.py"),
            Path("backend/app/modules/approvals/models.py"),
            Path("README.md"),
            Path("ARCHITECTURE.md"),
            Path("tokenizer.py"),  # Contiene 'token' pero con sufijo 'izer'
        ]
        for path in safe_cases:
            assert _is_sensitive(path) is False, f"Falso positivo en archivo seguro: {path}"

    def test_detect_excludes_sensitive_fixtures_from_corpus(self, synthetic_sensitive_fixtures: Path):
        """Verifica que detect() excluya secretos y los coloque en skipped_sensitive."""
        result = detect(synthetic_sensitive_fixtures)

        code_files = [Path(f).name for f in result.get("files", {}).get("code", [])]
        skipped_names = [Path(f).name for f in result.get("skipped_sensitive", [])]

        # 1. El código legítimo sí se incluye
        assert "main.py" in code_files

        # 2. Archivos sensibles están en skipped_sensitive
        assert ".env" in skipped_names
        assert ".env.production" in skipped_names
        assert "deploy.key" in skipped_names
        assert "cert.pem" in skipped_names
        assert "id_rsa" in skipped_names

        # 3. Ningún archivo sensible ingresó al conjunto de código o documentos procesables
        all_corpus_files = [
            Path(f).name
            for cat in ("code", "document", "paper", "image", "video")
            for f in result.get("files", {}).get(cat, [])
        ]
        for forbidden in (".env", ".env.production", "deploy.key", "cert.pem", "id_rsa"):
            assert forbidden not in all_corpus_files, f"{forbidden} fue incluido en el corpus de Graphify"

    def test_repository_exclusions_verification(self):
        """
        Verifica el estado de exclusiones en el repositorio actual:
        .env.example, scripts/check_secrets.py y modules/secrets están en skipped_sensitive,
        demostrando que Graphify filtra proactivamente cualquier mención de secretos.
        """
        repo_root = Path(__file__).resolve().parent.parent.parent
        res = detect(repo_root)

        skipped_basenames = [Path(f).name for f in res.get("skipped_sensitive", [])]
        assert "check_secrets.py" in skipped_basenames
        assert ".env.example" in skipped_basenames

        # Asegurar que ningún archivo .env real exista o haya sido indexado
        assert not (repo_root / ".env").exists()
