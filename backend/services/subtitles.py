from __future__ import annotations

from typing import Dict, List

from fastapi import HTTPException

from backend.services import jobs


def _fmt_time(seconds: float) -> str:
    millis = int(seconds * 1000)
    hours = millis // 3600000
    minutes = (millis % 3600000) // 60000
    secs = (millis % 60000) // 1000
    ms = millis % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _segments_to_srt(segments: List[Dict]) -> str:
    lines: List[str] = []
    for index, segment in enumerate(segments, start=1):
        lines.append(str(index))
        lines.append(f"{_fmt_time(segment['start'])} --> {_fmt_time(segment['end'])}")
        lines.append(segment["text"])
        lines.append("")
    return "\n".join(lines)


def _segments_to_txt(segments: List[Dict]) -> str:
    return "\n".join(segment["text"] for segment in segments)


def generate_subtitles(job_id: str) -> List[Dict]:
    """Monta a legenda a partir do que a dublagem ja produziu.

    Antes isso rodava um Whisper proprio no audio original, o que baixava mais um
    modelo, levava mais de um minuto e devolvia a legenda em INGLES. Agora a dublagem
    ja reconheceu a fala (Parakeet) e traduziu (Qwen3), com os tempos de cada bloco --
    a legenda sai em portugues, na hora e sem modelo extra.
    """
    segments_path = jobs.dub_segments_path(job_id)
    if not segments_path.exists():
        raise HTTPException(
            status_code=409,
            detail="Duble o vídeo primeiro: a legenda vem da própria dublagem.",
        )
    dub_segments = jobs.load_json(segments_path)
    # Jobs dublados por versoes anteriores nao guardavam o texto, so os tempos.
    if dub_segments and not any((s.get("text_pt") or "").strip() for s in dub_segments):
        raise HTTPException(
            status_code=409,
            detail=(
                "Este vídeo foi dublado por uma versão anterior do OpenDub. "
                "Duble novamente para gerar a legenda."
            ),
        )
    segments: List[Dict] = []
    for segment in dub_segments:
        texto = (segment.get("text_pt") or "").strip()
        if not texto:
            continue
        start = float(segment["start"])
        # A legenda acompanha a VOZ DUBLADA, que costuma durar mais que a fala original:
        # usar o fim do trecho em ingles cortava a legenda antes de a frase acabar.
        falado_s = float(segment.get("translated_ms") or 0.0) / 1000.0
        fim = start + falado_s if falado_s > 0 else float(segment["end"])
        segments.append(
            {
                "start": start,
                "end": max(fim, start + 0.3),
                "text": texto,
                "text_en": (segment.get("text_en") or "").strip(),
            }
        )
    if not segments:
        raise HTTPException(
            status_code=422,
            detail="Não há falas reconhecidas neste vídeo para gerar a legenda.",
        )
    jobs.save_json(jobs.transcription_path(job_id), segments)
    jobs.subtitles_srt_path(job_id).write_text(_segments_to_srt(segments), encoding="utf-8")
    jobs.transcript_txt_path(job_id).write_text(_segments_to_txt(segments), encoding="utf-8")
    return segments
