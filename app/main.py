"""Servidor web del Transcriptor."""

import asyncio
import hashlib
import hmac
import logging
import mimetypes
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, formats, transcriber, uploads
from .jobs import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("transcriptor")

LANGUAGES = {
    "auto": "Automático",
    "es": "Español", "en": "Inglés", "pt": "Portugués", "fr": "Francés",
    "it": "Italiano", "de": "Alemán", "ca": "Catalán", "nl": "Neerlandés",
    "ru": "Ruso", "ja": "Japonés", "zh": "Chino", "ko": "Coreano", "ar": "Árabe",
}

MODEL_INFO = {
    "tiny": "Muy rápido, precisión baja",
    "base": "Rápido, precisión básica",
    "small": "Equilibrado (recomendado en CPU)",
    "medium": "Preciso, lento",
    "large-v3-turbo": "Muy preciso y más rápido que large",
    "large-v3": "Máxima precisión, el más lento",
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    manager.load()
    manager.start()
    # Precarga el modelo por defecto para que el primer trabajo no espere la descarga.
    threading.Thread(target=transcriber.get_model, args=(config.DEFAULT_MODEL,), daemon=True).start()
    yield


app = FastAPI(title="Transcriptor", lifespan=lifespan, docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------- #
# Autenticación (solo si APP_PASSWORD está definida)
# --------------------------------------------------------------------------- #

COOKIE = "transcriptor_session"
_TOKEN = hmac.new(config.SESSION_SECRET.encode(), config.APP_PASSWORD.encode(), hashlib.sha256).hexdigest()
PUBLIC_PATHS = {"/api/config", "/api/login", "/healthz"}


def _authorized(request: Request) -> bool:
    if not config.APP_PASSWORD:
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), _TOKEN)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_PATHS and not _authorized(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
    return await call_next(request)


class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
async def login(body: LoginBody, request: Request):
    if not config.APP_PASSWORD:
        return {"ok": True}
    if not hmac.compare_digest(body.password.encode(), config.APP_PASSWORD.encode()):
        await asyncio.sleep(1.5)  # frena ataques de fuerza bruta
        raise HTTPException(401, "Contraseña incorrecta")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        COOKIE, _TOKEN, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax",
        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
    )
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


# --------------------------------------------------------------------------- #
# Configuración y salud
# --------------------------------------------------------------------------- #

@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/config")
async def get_config(request: Request):
    return {
        "auth_required": bool(config.APP_PASSWORD),
        "authenticated": _authorized(request),
        "chunk_size": config.CHUNK_SIZE,
        "default_model": config.DEFAULT_MODEL,
        "models": [{"id": m, "label": MODEL_INFO.get(m, "")} for m in config.ALLOWED_MODELS],
        "languages": [{"id": k, "label": v} for k, v in LANGUAGES.items()],
        "retention_hours": config.RETENTION_HOURS,
    }


# --------------------------------------------------------------------------- #
# Subidas
# --------------------------------------------------------------------------- #

def _upload_error(e: uploads.UploadError) -> JSONResponse:
    return JSONResponse({"detail": e.message, "received": e.received}, status_code=e.status)


class UploadInit(BaseModel):
    filename: str
    size: int


@app.post("/api/uploads")
async def upload_create(body: UploadInit):
    try:
        return uploads.create(body.filename, body.size)
    except uploads.UploadError as e:
        return _upload_error(e)


@app.get("/api/uploads/{upload_id}")
async def upload_status(upload_id: str):
    try:
        m = uploads.meta(upload_id)
        return {"received": m["received"], "size": m["size"], "chunk_size": config.CHUNK_SIZE}
    except uploads.UploadError as e:
        return _upload_error(e)


@app.put("/api/uploads/{upload_id}")
async def upload_chunk(upload_id: str, offset: int, request: Request):
    try:
        received = await uploads.write_chunk(upload_id, offset, request.stream())
        return {"received": received}
    except uploads.UploadError as e:
        return _upload_error(e)


class UploadComplete(BaseModel):
    model: str
    language: str = "auto"


@app.post("/api/uploads/{upload_id}/complete")
async def upload_complete(upload_id: str, body: UploadComplete):
    if body.model not in config.ALLOWED_MODELS:
        raise HTTPException(400, "Modelo no permitido")
    if body.language not in LANGUAGES:
        raise HTTPException(400, "Idioma no soportado")
    try:
        path, m = uploads.finish(upload_id)
    except uploads.UploadError as e:
        return _upload_error(e)
    job = manager.create(path, m["filename"], m["size"], body.model,
                         None if body.language == "auto" else body.language)
    uploads.discard(upload_id)
    return job.public()


@app.delete("/api/uploads/{upload_id}")
async def upload_delete(upload_id: str):
    uploads.discard(upload_id)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Trabajos
# --------------------------------------------------------------------------- #

def _job_or_404(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "Trabajo no encontrado")
    return job


@app.get("/api/jobs")
async def jobs_list():
    return manager.list()


@app.get("/api/jobs/{job_id}")
async def job_detail(job_id: str, since: int = 0):
    job = _job_or_404(job_id)
    data = job.public()
    data["queue_position"] = manager.queue_position(job_id)
    data["segments"] = job.segments[max(since, 0):]
    data["since"] = max(since, 0)
    return data


@app.post("/api/jobs/{job_id}/cancel")
async def job_cancel(job_id: str):
    _job_or_404(job_id)
    manager.cancel(job_id)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
async def job_delete(job_id: str):
    _job_or_404(job_id)
    manager.delete(job_id)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/media")
async def job_media(job_id: str):
    job = _job_or_404(job_id)
    if not job.source.exists():
        raise HTTPException(404, "Archivo no disponible")
    media_type = mimetypes.guess_type(job.data["filename"])[0] or "application/octet-stream"
    return FileResponse(job.source, media_type=media_type)


@app.get("/api/jobs/{job_id}/download/{fmt}")
async def job_download(job_id: str, fmt: str):
    job = _job_or_404(job_id)
    stem = Path(job.data["filename"]).stem or "transcripcion"
    if fmt == "json":
        content, media_type = formats.to_json(job.public(), job.segments), "application/json"
    elif fmt in formats.FORMATS:
        media_type, fn = formats.FORMATS[fmt]
        content = fn(job.segments)
    else:
        raise HTTPException(404, "Formato no soportado")
    name = f"{stem}.{fmt}"
    return Response(
        content.encode("utf-8"),
        media_type=f"{media_type}; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


# La interfaz web (debe montarse al final para no tapar las rutas /api).
app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
