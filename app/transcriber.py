"""Extracción de audio y transcripción con faster-whisper.

Flujo:
1. `extract_audio` decodifica solo la pista de audio del archivo (video o audio,
   cualquier formato que soporte FFmpeg vía PyAV) y la escribe en disco como WAV
   mono de 16 kHz, sin cargarla entera en memoria.
2. `transcribe_wav` recorre ese WAV en ventanas de WINDOW_SECONDS, cortando cada
   ventana en el punto más silencioso cercano para no partir palabras. Así la RAM
   usada es la misma para 5 minutos que para 10 horas.
"""

import logging
import threading
import wave
from pathlib import Path
from typing import Callable

import av
import numpy as np
from faster_whisper import WhisperModel

from . import config

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000


class Cancelled(Exception):
    pass


class NoAudioError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #

def extract_audio(
    src: Path,
    dst_wav: Path,
    on_progress: Callable[[float], None],
    is_cancelled: Callable[[], bool],
) -> float:
    """Convierte `src` a WAV 16 kHz mono. Devuelve la duración en segundos."""
    try:
        container = av.open(str(src))
    except av.error.FFmpegError as e:
        raise NoAudioError(
            "No se pudo leer el archivo: está dañado o no es un formato de audio/video reconocido."
        ) from e

    with container:
        if not container.streams.audio:
            raise NoAudioError("El archivo no contiene ninguna pista de audio.")
        stream = container.streams.audio[0]
        stream.thread_type = "AUTO"

        total = None
        if stream.duration and stream.time_base:
            total = float(stream.duration * stream.time_base)
        elif container.duration:
            total = container.duration / av.time_base

        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        written = 0
        last_report = 0.0

        with wave.open(str(dst_wav), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(SAMPLE_RATE)

            def write(frames):
                nonlocal written
                for f in frames:
                    data = f.to_ndarray().tobytes()
                    out.writeframes(data)
                    written += len(data) // 2

            for packet in container.demux(stream):
                try:
                    frames = packet.decode()
                except av.error.FFmpegError:
                    continue  # paquete dañado: se omite y se sigue
                for frame in frames:
                    write(resampler.resample(frame))

                pos = written / SAMPLE_RATE
                if pos - last_report >= 5:
                    last_report = pos
                    if is_cancelled():
                        raise Cancelled()
                    if total:
                        on_progress(min(pos / total, 1.0))
            write(resampler.resample(None))

    if written == 0:
        raise NoAudioError("No se pudo decodificar audio del archivo.")
    return written / SAMPLE_RATE


def _open_pcm(wav_path: Path) -> np.ndarray:
    """Mapea las muestras del WAV a memoria sin leerlas todas."""
    with wave.open(str(wav_path), "rb") as w:
        n = w.getnframes()
    offset = wav_path.stat().st_size - n * 2
    return np.memmap(wav_path, dtype=np.int16, mode="r", offset=offset, shape=(n,))


def _quietest_point(pcm: np.ndarray, start: int, end: int) -> int:
    """Punto silencioso (en bloques de 100 ms) entre start y end.

    Entre los bloques casi tan silenciosos como el mínimo se elige el último,
    para que la ventana quede lo más cerca posible del tamaño previsto.
    """
    block = SAMPLE_RATE // 10
    region = np.asarray(pcm[start:end], dtype=np.float32)
    n_blocks = len(region) // block
    if n_blocks < 2:
        return end
    energy = (region[: n_blocks * block].reshape(n_blocks, block) ** 2).mean(axis=1)
    quiet = np.flatnonzero(energy <= energy.min() * 1.5 + 1.0)
    return start + int(quiet[-1]) * block + block // 2


# --------------------------------------------------------------------------- #
# Modelo
# --------------------------------------------------------------------------- #

_model_lock = threading.Lock()
_model: WhisperModel | None = None
_model_name: str | None = None


def get_model(name: str) -> WhisperModel:
    """Carga (y descarga la primera vez) el modelo. Solo uno en memoria a la vez."""
    global _model, _model_name
    with _model_lock:
        if _model is None or _model_name != name:
            _model = None  # libera el anterior antes de cargar el nuevo
            log.info("Cargando modelo %s (%s, %s)", name, config.DEVICE, config.COMPUTE_TYPE)
            _model = WhisperModel(
                name,
                device=config.DEVICE,
                compute_type=config.COMPUTE_TYPE,
                cpu_threads=config.CPU_THREADS,
                download_root=str(config.MODELS_DIR),
            )
            _model_name = name
        return _model


# --------------------------------------------------------------------------- #
# Transcripción
# --------------------------------------------------------------------------- #

def transcribe_wav(
    wav_path: Path,
    model_name: str,
    language: str | None,
    on_segment: Callable[[dict], None],
    on_progress: Callable[[float], None],
    on_language: Callable[[str, float], None],
    is_cancelled: Callable[[], bool],
) -> None:
    model = get_model(model_name)
    pcm = _open_pcm(wav_path)
    total = len(pcm)
    if total == 0:
        return

    window = config.WINDOW_SECONDS * SAMPLE_RATE
    search = 30 * SAMPLE_RATE  # buscar el corte en los últimos 30 s de la ventana
    start = 0
    prev_text = ""

    while start < total:
        if is_cancelled():
            raise Cancelled()
        end = start + window
        if end >= total or total - end < 60 * SAMPLE_RATE:
            end = total  # no dejar una cola de menos de 1 minuto
        else:
            end = _quietest_point(pcm, end - search, end)

        audio = np.asarray(pcm[start:end], dtype=np.float32) / 32768.0
        offset = start / SAMPLE_RATE

        segments, info = model.transcribe(
            audio,
            language=language,
            beam_size=config.BEAM_SIZE,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            # Ajusta inicio/fin de cada segmento al habla real (sin esto Whisper
            # estira los tiempos sobre los silencios y los subtítulos se desfasan).
            word_timestamps=True,
            # Evita bucles de repetición, el fallo más típico de Whisper en audios largos.
            condition_on_previous_text=False,
            # Mantiene estilo y vocabulario entre ventanas.
            initial_prompt=prev_text[-200:] or None,
        )

        if language is None:
            # El idioma detectado en la primera ventana se fija para el resto,
            # para que no cambie a mitad de la transcripción.
            on_language(info.language, info.language_probability)
            if info.language_probability >= 0.5:
                language = info.language

        for seg in segments:
            if is_cancelled():
                raise Cancelled()
            text = seg.text.strip()
            if not text:
                continue
            seg_end = offset + seg.end
            on_segment({"start": round(offset + seg.start, 2), "end": round(seg_end, 2), "text": text})
            prev_text = text
            on_progress(min(seg_end * SAMPLE_RATE / total, 1.0))

        start = end
        on_progress(min(start / total, 1.0))
