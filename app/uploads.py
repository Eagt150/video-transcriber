"""Subidas por trozos y reanudables.

Protocolo:
  POST /api/uploads                   {filename, size}  -> {id, received, chunk_size}
  GET  /api/uploads/{id}                                -> {received}
  PUT  /api/uploads/{id}?offset=N     cuerpo = bytes del trozo
  POST /api/uploads/{id}/complete     {model, language} -> trabajo creado

Cada trozo es una petición HTTP pequeña, así que no hay límite práctico de tamaño
de archivo y se esquiva el límite de 100 MB por petición de Cloudflare. Si la
conexión se corta, el navegador pregunta cuántos bytes llegaron y continúa.
"""

import json
import re
import shutil
import uuid
from pathlib import Path

from . import config

_ID_RE = re.compile(r"^[a-f0-9]{32}$")


class UploadError(Exception):
    def __init__(self, status: int, message: str, received: int | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.received = received


def _dir(upload_id: str) -> Path:
    if not _ID_RE.match(upload_id):
        raise UploadError(404, "Subida no encontrada")
    d = config.UPLOADS_DIR / upload_id
    if not d.exists():
        raise UploadError(404, "Subida no encontrada o caducada")
    return d


def meta(upload_id: str, owner: str) -> dict:
    d = _dir(upload_id)
    m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    if m.get("owner") != owner:
        raise UploadError(404, "Subida no encontrada")
    m["received"] = (d / "data").stat().st_size
    return m


def create(filename: str, size: int, owner: str) -> dict:
    filename = Path(filename).name.strip() or "archivo"
    if size <= 0:
        raise UploadError(400, "El archivo está vacío")
    if config.MAX_UPLOAD_BYTES and size > config.MAX_UPLOAD_BYTES:
        raise UploadError(413, f"El archivo supera el máximo de {config.MAX_UPLOAD_BYTES // 1024**3} GB")
    free = shutil.disk_usage(config.DATA_DIR).free
    if size > free - 512 * 1024**2:
        raise UploadError(507, "No hay espacio suficiente en el servidor para este archivo")

    upload_id = uuid.uuid4().hex
    d = config.UPLOADS_DIR / upload_id
    d.mkdir(parents=True)
    (d / "data").touch()
    m = {"id": upload_id, "filename": filename, "size": size, "owner": owner}
    (d / "meta.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return {"id": upload_id, "filename": filename, "size": size, "received": 0, "chunk_size": config.CHUNK_SIZE}


async def write_chunk(upload_id: str, owner: str, offset: int, stream) -> int:
    m = meta(upload_id, owner)
    data_path = _dir(upload_id) / "data"
    if offset != m["received"]:
        raise UploadError(409, "Posición incorrecta", received=m["received"])

    written = 0
    limit = config.CHUNK_SIZE + 1024 * 1024
    with open(data_path, "ab") as f:
        try:
            async for part in stream:
                written += len(part)
                if written > limit or offset + written > m["size"]:
                    raise UploadError(413, "Trozo demasiado grande")
                f.write(part)
        except UploadError:
            f.truncate(offset)
            raise
        except Exception:
            # Conexión cortada a mitad de trozo: se descarta el trozo incompleto.
            f.truncate(offset)
            raise UploadError(400, "Trozo incompleto", received=offset)
    return offset + written


def finish(upload_id: str, owner: str) -> tuple[Path, dict]:
    m = meta(upload_id, owner)
    if m["received"] != m["size"]:
        raise UploadError(409, "La subida no está completa", received=m["received"])
    d = _dir(upload_id)
    return d / "data", m


def discard(upload_id: str, owner: str) -> None:
    try:
        d = _dir(upload_id)
        m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (UploadError, OSError, ValueError):
        return
    if m.get("owner") == owner:
        shutil.rmtree(d, ignore_errors=True)
