"""Cola de trabajos de transcripción, persistida en disco.

Cada trabajo vive en data/jobs/<id>/ con:
  job.json       estado y metadatos
  source.<ext>   archivo original (para reproducirlo en el visor; se borra al terminar)
  audio.wav      audio extraído, 16 kHz mono
  segments.json  segmentos transcritos
"""

import json
import logging
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import config, transcriber

log = logging.getLogger(__name__)

FINAL = {"done", "error", "cancelled"}


def _write_json(path: Path, data) -> None:
    if not path.parent.exists():
        return  # el trabajo se borró mientras se procesaba
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except FileNotFoundError:
        pass
    except OSError:
        # Con el disco lleno no se puede guardar, pero el estado sigue en memoria y el
        # hilo de transcripción no debe morir (si muere, ningún trabajo más se procesa).
        log.exception("No se pudo guardar %s", path)


class Job:
    def __init__(self, data: dict, segments: list[dict] | None = None):
        self.data = data
        self.segments: list[dict] = segments or []
        self.cancel_requested = False
        self._last_save = 0.0
        self._last_seg_save = 0.0

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def dir(self) -> Path:
        return config.JOBS_DIR / self.id

    @property
    def source(self) -> Path:
        return self.dir / self.data["source_name"]

    @property
    def wav(self) -> Path:
        return self.dir / "audio.wav"

    def drop_source(self) -> None:
        """Borra el archivo original: la transcripción ya no lo necesita y ocupa casi todo el disco."""
        self.source.unlink(missing_ok=True)

    def update(self, force: bool = False, **fields) -> None:
        self.data.update(fields)
        now = time.time()
        if force or now - self._last_save > 3:
            self._last_save = now
            _write_json(self.dir / "job.json", self.data)

    def save_segments(self, force: bool = False) -> None:
        now = time.time()
        if force or now - self._last_seg_save > 20:
            self._last_seg_save = now
            _write_json(self.dir / "segments.json", self.segments)

    def public(self) -> dict:
        d = dict(self.data)
        d.pop("owner", None)
        d["has_media"] = self.source.exists()
        d["segment_count"] = len(self.segments)
        return d


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.queue: "queue.Queue[str]" = queue.Queue()
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- arranque

    def load(self) -> None:
        """Recupera trabajos del disco; los que quedaron a medias se reencolan."""
        pending = []
        for d in sorted(config.JOBS_DIR.iterdir()):
            try:
                data = json.loads((d / "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            segs = []
            seg_file = d / "segments.json"
            if data["status"] == "done" and seg_file.exists():
                try:
                    segs = json.loads(seg_file.read_text(encoding="utf-8"))
                except ValueError:
                    pass
            job = Job(data, segs)
            if data["status"] in FINAL:
                job.drop_source()
            else:
                job.update(force=True, status="queued", stage="En cola (reanudado tras reinicio)", progress=0)
                pending.append(job)
            self.jobs[job.id] = job
        for job in sorted(pending, key=lambda j: j.data["created_at"]):
            self.queue.put(job.id)

    def start(self) -> None:
        threading.Thread(target=self._worker, name="transcriber", daemon=True).start()
        threading.Thread(target=self._cleaner, name="cleaner", daemon=True).start()

    # ---------------------------------------------------------------- API

    def create(self, upload_file: Path, filename: str, size: int, model: str, language: str | None,
               owner: str) -> Job:
        job_id = uuid.uuid4().hex[:12]
        ext = Path(filename).suffix.lower()[:10]
        job_dir = config.JOBS_DIR / job_id
        job_dir.mkdir(parents=True)
        source_name = f"source{ext}"
        shutil.move(str(upload_file), job_dir / source_name)

        job = Job({
            "id": job_id,
            "owner": owner,
            "filename": filename,
            "source_name": source_name,
            "size": size,
            "model": model,
            "language": language,
            "detected_language": None,
            "language_probability": None,
            "status": "queued",
            "stage": "En cola",
            "progress": 0.0,
            "eta_seconds": None,
            "duration": None,
            "error": None,
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
        })
        job.update(force=True)
        with self.lock:
            self.jobs[job_id] = job
        self.queue.put(job_id)
        return job

    def get(self, job_id: str, owner: str) -> Job | None:
        """Solo devuelve el trabajo a quien lo creó."""
        job = self.jobs.get(job_id)
        return job if job and job.data.get("owner") == owner else None

    def list(self, owner: str) -> list[dict]:
        with self.lock:
            jobs = [j for j in self.jobs.values() if j.data.get("owner") == owner]
        jobs.sort(key=lambda j: j.data["created_at"], reverse=True)
        return [j.public() for j in jobs]

    def queue_position(self, job_id: str) -> int | None:
        with self.lock:
            queued = sorted(
                (j for j in self.jobs.values() if j.data["status"] == "queued"),
                key=lambda j: j.data["created_at"],
            )
        for i, j in enumerate(queued):
            if j.id == job_id:
                return i + 1
        return None

    def cancel(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job or job.data["status"] in FINAL:
            return
        job.cancel_requested = True
        job.update(force=True, stage="Cancelando…")
        if job.data["status"] == "queued":
            job.update(force=True, status="cancelled", stage="Cancelado", finished_at=time.time())

    def delete(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        job.cancel_requested = True
        with self.lock:
            self.jobs.pop(job_id, None)
        shutil.rmtree(job.dir, ignore_errors=True)

    # ---------------------------------------------------------------- worker

    def _worker(self) -> None:
        while True:
            job_id = self.queue.get()
            job = self.jobs.get(job_id)
            if not job or job.cancel_requested or job.data["status"] != "queued":
                continue
            try:
                self._run(job)
            except transcriber.Cancelled:
                job.save_segments(force=True)
                job.update(force=True, status="cancelled", stage="Cancelado",
                           eta_seconds=None, finished_at=time.time())
            except transcriber.NoAudioError as e:
                job.update(force=True, status="error", stage="Error", error=str(e), finished_at=time.time())
            except Exception as e:  # noqa: BLE001 - cualquier fallo debe quedar visible en la UI
                log.exception("Fallo en el trabajo %s", job_id)
                job.update(force=True, status="error", stage="Error",
                           error=f"{type(e).__name__}: {e}", finished_at=time.time())
            finally:
                if job.dir.exists():
                    job.wav.unlink(missing_ok=True)
                    if job.data["status"] in FINAL:
                        job.drop_source()

    def _run(self, job: Job) -> None:
        is_cancelled = lambda: job.cancel_requested  # noqa: E731
        job.segments = []
        job.update(force=True, status="extracting", stage="Extrayendo audio",
                   progress=0.0, started_at=time.time(), error=None)

        duration = transcriber.extract_audio(
            job.source, job.wav,
            on_progress=lambda p: job.update(progress=p),
            is_cancelled=is_cancelled,
        )

        job.update(force=True, status="transcribing", duration=round(duration, 2),
                   stage=f"Preparando el modelo {job.data['model']}", progress=0.0)
        t0 = None

        def on_progress(p: float) -> None:
            nonlocal t0
            eta = None
            if t0 is None:
                t0 = time.time()
            elif p > 0.01:
                elapsed = time.time() - t0
                eta = round(elapsed / p * (1 - p))
            stage = "Cancelando…" if job.cancel_requested else "Transcribiendo"
            job.update(progress=round(p, 4), eta_seconds=eta, stage=stage)

        def on_segment(seg: dict) -> None:
            job.segments.append(seg)
            job.save_segments()

        def on_language(lang: str, prob: float) -> None:
            job.update(force=True, detected_language=lang, language_probability=round(prob, 3))

        transcriber.transcribe_wav(
            job.wav, job.data["model"], job.data["language"],
            on_segment=on_segment, on_progress=on_progress,
            on_language=on_language, is_cancelled=is_cancelled,
        )
        job.save_segments(force=True)
        job.update(force=True, status="done", stage="Completado", progress=1.0,
                   eta_seconds=None, finished_at=time.time())

    # ---------------------------------------------------------------- limpieza

    def _cleaner(self) -> None:
        while True:
            try:
                limit = time.time() - config.RETENTION_HOURS * 3600
                for job in list(self.jobs.values()):
                    if job.data["status"] in FINAL and (job.data["finished_at"] or 0) < limit:
                        log.info("Borrando trabajo antiguo %s", job.id)
                        self.delete(job.id)
                upload_limit = time.time() - 24 * 3600
                for d in config.UPLOADS_DIR.iterdir():
                    if d.stat().st_mtime < upload_limit:
                        shutil.rmtree(d, ignore_errors=True)
            except Exception:  # noqa: BLE001
                log.exception("Error en la limpieza")
            time.sleep(1800)


manager = JobManager()
