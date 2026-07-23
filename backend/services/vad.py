from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, TypedDict

import torch
from torchaudio.functional import resample
from silero_vad import get_speech_timestamps, load_silero_vad

from backend.services import media

VAD_SAMPLE_RATE = 16000
MIN_SILENCE_DURATION_MS = 350
SPEECH_ONLY_MIN_SILENCE_MS = int(os.getenv("SPEECH_ONLY_MIN_SILENCE_MS", "100"))
SPEECH_ONLY_PAD_MS = int(os.getenv("SPEECH_ONLY_PAD_MS", "30"))
# Blocos um pouco maiores diminuem chamadas ao modelo sem misturar falas muito longas.
MAX_SPEECH_DURATION_S = float(os.getenv("MAX_CHUNK_DURATION_S", "16.0"))
PAD_S = 0.08

_model = None


class Chunk(TypedDict):
    start: float
    end: float


def get_model():
    global _model
    if _model is None:
        _model = load_silero_vad()
    return _model


def load_16k_mono(path: Path) -> torch.Tensor:
    wav, sample_rate = media.read_wav(path)
    if wav.shape[0] > 1:
        wav = wav.mean(0, keepdim=True)
    if sample_rate != VAD_SAMPLE_RATE:
        wav = resample(wav, sample_rate, VAD_SAMPLE_RATE)
    return wav.squeeze(0)


def get_chunks(wav: torch.Tensor, pad_s: float = PAD_S) -> List[Chunk]:
    """Detecta trechos de fala num tensor mono 16kHz ja carregado (ver load_16k_mono).

    Devolve blocos com padding simetrico, sem sobreposicao. min_silence_duration_ms
    generoso mantem pausas curtas DENTRO de um bloco, o que evita duplicar essa pausa
    depois (o SeamlessM4T ja preserva a pausa proporcional no audio gerado para aquele
    bloco).
    """
    model = get_model()
    timestamps = get_speech_timestamps(
        wav,
        model,
        sampling_rate=VAD_SAMPLE_RATE,
        min_silence_duration_ms=MIN_SILENCE_DURATION_MS,
        max_speech_duration_s=MAX_SPEECH_DURATION_S,
        return_seconds=True,
    )
    total_duration = wav.shape[-1] / VAD_SAMPLE_RATE

    chunks: List[Chunk] = []
    for ts in timestamps:
        start = max(0.0, float(ts["start"]) - pad_s)
        end = min(total_duration, float(ts["end"]) + pad_s)
        if chunks and start < chunks[-1]["end"]:
            start = chunks[-1]["end"]
        if end > start:
            chunks.append({"start": start, "end": end})
    return chunks


def slice_audio(wav: torch.Tensor, start: float, end: float, sample_rate: int = VAD_SAMPLE_RATE) -> torch.Tensor:
    start_sample = int(start * sample_rate)
    end_sample = int(end * sample_rate)
    return wav[start_sample:end_sample]


def remove_silence(wav: torch.Tensor) -> torch.Tensor:
    """Concatena somente as regioes de fala detectadas em um trecho ja limpo.

    Este segundo VAD e mais sensivel que o usado para formar blocos: pausas a partir
    de 100 ms sao removidas antes do envio ao SeamlessM4T. Um padding curto evita
    cortar consoantes no inicio/fim das palavras.
    """
    timestamps = get_speech_timestamps(
        wav,
        get_model(),
        sampling_rate=VAD_SAMPLE_RATE,
        min_silence_duration_ms=SPEECH_ONLY_MIN_SILENCE_MS,
        speech_pad_ms=SPEECH_ONLY_PAD_MS,
        return_seconds=False,
    )
    speech_parts = [wav[int(ts["start"]):int(ts["end"])] for ts in timestamps]
    speech_parts = [part for part in speech_parts if part.numel() > 0]
    if not speech_parts:
        return torch.empty(0, dtype=wav.dtype)
    return torch.cat(speech_parts)
