"""Limited, offline source check. Not a comprehensive credential scanner."""
import re
import sys
from pathlib import Path

PATTERNS = (
    re.compile(r"(?:sk-|key-)[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r'''(?i)\b(?:password|api_key|secret_key)\s*[:=]\s*["'][^"'\n]+["']'''),
)
SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml"}


def scan(paths):
    """Return 0 for clean, 1 for findings, 2 for incomplete scan."""
    found = False
    try:
        for root in paths:
            root = Path(root)
            if not root.is_dir():
                raise OSError("missing source directory")
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    raise OSError("symlink not supported")
                if not path.is_file() or path.suffix not in SUFFIXES:
                    continue
                for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if any(pattern.search(line) for pattern in PATTERNS):
                        # Never emit matching lines or credential values.
                        print(f"Possible hardcoded secret: {path}:{line_number}")
                        found = True
    except (OSError, UnicodeError):
        print("Secret check incomplete: source could not be safely read.", file=sys.stderr)
        return 2
    if found:
        return 1
    print("No matching secrets in scanned application source (limited patterns).")
    return 0


if __name__ == "__main__":
    sys.exit(scan(sys.argv[1:] or ["backend/app", "scripts"]))
