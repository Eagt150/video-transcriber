"""Configuración leída de variables de entorno (ver .env.example)."""

import os
import secrets
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data")).resolve()
UPLOADS_DIR = DATA_DIR / "uploads"
JOBS_DIR = DATA_DIR / "jobs"
MODELS_DIR = Path(os.environ.get("MODELS_DIR", DATA_DIR / "models")).resolve()
STATIC_DIR = BASE_DIR / "static"

# Modelo por defecto y modelos que se ofrecen en la interfaz.
DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "small")
ALLOWED_MODELS = [
    m.strip()
    for m in os.environ.get("ALLOWED_MODELS", "tiny,base,small,medium,large-v3-turbo,large-v3").split(",")
    if m.strip()
]
if DEFAULT_MODEL not in ALLOWED_MODELS:
    ALLOWED_MODELS.insert(0, DEFAULT_MODEL)

# CPU: int8 es el tipo más rápido y con menos RAM. En GPU usar "float16".
DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
CPU_THREADS = _int("CPU_THREADS", os.cpu_count() or 4)
BEAM_SIZE = _int("BEAM_SIZE", 5)

# El audio se transcribe por ventanas para que la RAM no crezca con la duración.
WINDOW_SECONDS = _int("WINDOW_SECONDS", 20 * 60)

# Subida por trozos. Cloudflare (plan gratis) rechaza peticiones de más de 100 MB,
# así que cada trozo debe ser bastante menor.
CHUNK_SIZE = _int("CHUNK_SIZE_MB", 32) * 1024 * 1024
MAX_UPLOAD_BYTES = _int("MAX_UPLOAD_GB", 0) * 1024**3  # 0 = sin límite

# Limpieza automática de trabajos y subidas abandonadas.
RETENTION_HOURS = _int("RETENTION_HOURS", 72)

# Contraseña opcional (recomendada al publicar en internet).
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
# Si no se define, se genera una por arranque (las sesiones caducan al reiniciar).
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)

for d in (UPLOADS_DIR, JOBS_DIR, MODELS_DIR):
    d.mkdir(parents=True, exist_ok=True)
