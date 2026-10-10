"""Conversión de segmentos a TXT, SRT, VTT y JSON."""

import json


def _ts(seconds: float, sep: str) -> str:
    ms = int(round(max(seconds, 0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_txt(segments: list[dict]) -> str:
    """Texto corrido; una pausa de más de 2 s inicia un párrafo nuevo."""
    paragraphs: list[list[str]] = []
    last_end = None
    for seg in segments:
        if last_end is None or seg["start"] - last_end > 2.0:
            paragraphs.append([])
        paragraphs[-1].append(seg["text"])
        last_end = seg["end"]
    return "\n\n".join(" ".join(p) for p in paragraphs) + "\n"


def to_txt_timed(segments: list[dict]) -> str:
    return "".join(f"[{_ts(s['start'], '.')[:-4]}] {s['text']}\n" for s in segments)


def to_srt(segments: list[dict]) -> str:
    blocks = [
        f"{i}\n{_ts(s['start'], ',')} --> {_ts(s['end'], ',')}\n{s['text']}\n"
        for i, s in enumerate(segments, 1)
    ]
    return "\n".join(blocks)


def to_vtt(segments: list[dict]) -> str:
    blocks = [f"{_ts(s['start'], '.')} --> {_ts(s['end'], '.')}\n{s['text']}\n" for s in segments]
    return "WEBVTT\n\n" + "\n".join(blocks)


def to_json(job: dict, segments: list[dict]) -> str:
    return json.dumps({**job, "segments": segments}, ensure_ascii=False, indent=2)


FORMATS = {
    "txt": ("text/plain", to_txt),
    "tiempos.txt": ("text/plain", to_txt_timed),
    "srt": ("application/x-subrip", to_srt),
    "vtt": ("text/vtt", to_vtt),
}
