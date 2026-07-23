from __future__ import annotations

import os
from typing import Dict, List, Optional

from backend.services import jobs, media

WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")

_model = None


def get_model():
    """Import e carregamento do faster-whisper sao lazy de proposito: legenda e uma acao
    explicita do usuario (baixa modelo na primeira vez), nunca parte do fluxo de /dub.
    """
    global _model
    if _model is None:
        import torch
        from faster_whisper import WhisperModel

        # faster-whisper (ctranslate2) neste ambiente nao encontra as libs cublas da
        # stack CUDA 13 instalada para o torch/SeamlessM4T -- roda em CPU, o que e
        # aceitavel aqui pois legenda e uma acao pontual/infrequente, nao o fluxo principal.
        device = "cpu"
        compute_type = "int8"
        _model = WhisperModel(WHISPER_MODEL_SIZE, device=device, compute_type=compute_type)
    return _model


def _segments_to_srt(segments: List[Dict]) -> str:
    def fmt_time(seconds: float) -> str:
        millis = int(seconds * 1000)
        hours = millis // 3600000
        minutes = (millis % 3600000) // 60000
        secs = (millis % 60000) // 1000
        ms = millis % 1000
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"

    lines: List[str] = []
    for index, segment in enumerate(segments, start=1):
        lines.append(str(index))
        lines.append(f"{fmt_time(segment['start'])} --> {fmt_time(segment['end'])}")
        lines.append(segment["text"])
        lines.append("")
    return "\n".join(lines)


def _segments_to_txt(segments: List[Dict]) -> str:
    return "\n".join(segment["text"] for segment in segments)


def generate_subtitles(job_id: str) -> List[Dict]:
    """Transcreve o audio original (ingles) e salva transcription.json, subtitles.srt e
    transcript.txt.

    v1: legenda apenas no idioma original (sem traducao) -- mantem esse caminho
    independente do Argos/traducao, que saiu do fluxo principal de dublagem.
    """
    media_path = jobs.resolve_media_path(job_id)
    model = get_model()
    segments_iter, _ = model.transcribe(str(media_path), vad_filter=True, word_timestamps=True)
    segments: List[Dict] = [
        {"start": float(s.start), "end": float(s.end), "text": s.text.strip()}
        for s in segments_iter
    ]
    jobs.save_json(jobs.transcription_path(job_id), segments)
    jobs.subtitles_srt_path(job_id).write_text(_segments_to_srt(segments), encoding="utf-8")
    jobs.transcript_txt_path(job_id).write_text(_segments_to_txt(segments), encoding="utf-8")
    return segments
