"""Configuración leída de variables de entorno (ver .env.example)."""

import os
import secrets
import shutil
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _available_cpus() -> int:
    """Núcleos realmente asignados al contenedor (cgroup), no los de la máquina física."""
    count = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 4)
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            count = min(count, max(1, int(int(quota) / int(period))))
    except (OSError, ValueError):
        pass
    return count


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
CPU_THREADS = _int("CPU_THREADS", _available_cpus())
BEAM_SIZE = _int("BEAM_SIZE", 5)

# El audio se transcribe por ventanas para que la RAM no crezca con la duración.
WINDOW_SECONDS = _int("WINDOW_SECONDS", 20 * 60)

# Subida por trozos. Cloudflare (plan gratis) rechaza peticiones de más de 100 MB,
# así que cada trozo debe ser bastante menor.
CHUNK_SIZE = _int("CHUNK_SIZE_MB", 32) * 1024 * 1024
MAX_UPLOAD_BYTES = _int("MAX_UPLOAD_GB", 0) * 1024**3  # 0 = sin límite
# Espacio libre que se guarda para el audio extraído (~115 MB por hora de audio).
DISK_RESERVE_BYTES = _int("DISK_RESERVE_MB", 256) * 1024**2
# Si falta espacio, se borran las subidas que lleven estos minutos sin recibir datos.
UPLOAD_STALL_MINUTES = _int("UPLOAD_STALL_MINUTES", 30)

# Limpieza automática de trabajos y subidas abandonadas.
RETENTION_HOURS = _int("RETENTION_HOURS", 72)

# Contraseña opcional (recomendada al publicar en internet).
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
# Si no se define, se genera una por arranque (las sesiones caducan al reiniciar).
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)

for d in (UPLOADS_DIR, JOBS_DIR, MODELS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Si los modelos viven fuera de DATA_DIR, se borran copias viejas que ocupen el volumen.
if MODELS_DIR != DATA_DIR / "models" and (DATA_DIR / "models").is_dir():
    shutil.rmtree(DATA_DIR / "models", ignore_errors=True)
