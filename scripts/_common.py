"""Bootstrap bersama untuk script CLI: path import & logging."""
import logging
import sys
from pathlib import Path

import truststore

# Pakai cert store Windows (bukan bundle certifi) agar HTTPS lolos proxy/antivirus kantor yang memasang CA sendiri
truststore.inject_into_ssl()

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Konsol Windows default cp1252 tidak bisa menampilkan emoji di notifikasi
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def setup_logging(logfile: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if logfile:
        path = ROOT / logfile
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    for noisy in ("httpx", "httpx2", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
