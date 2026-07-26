from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any, Optional, Tuple

import torch
from torchaudio.functional import resample

from backend.services import jobs, media

_model: Optional[torch.nn.Module] = None
_state: Any = None

# O DeepFilterNet e uma rede recorrente: mandar o audio inteiro de uma vez faz o cuDNN
# recusar a sequencia (CUDNN_STATUS_NOT_SUPPORTED) em videos longos -- um video de 22 min
# vira 65 milhoes de amostras numa unica chamada. Processar em blocos resolve e nao muda
# o resultado, desde que as bordas sejam cruzadas.
CHUNK_SECONDS = 30.0
CROSSFADE_SECONDS = 0.5


def _get_model() -> Tuple[torch.nn.Module, Any]:
    """Carrega DeepFilterNet3 uma vez, com compatibilidade para torchaudio 2.9.

    DeepFilterNet 0.5.6 apenas importa ``AudioMetaData`` para anotacao de tipo de
    uma funcao que nao usamos; esse modulo deixou de existir no torchaudio 2.9.
    """
    global _model, _state
    if _model is None or _state is None:
        compat_name = "torchaudio.backend.common"
        if compat_name not in sys.modules:
            compat = types.ModuleType(compat_name)
            compat.AudioMetaData = Any
            sys.modules[compat_name] = compat
        from df.enhance import init_df

        _model, _state, _ = init_df(log_level="WARNING", log_file=None)
    return _model, _state


def unload_model() -> None:
    """Libera a VRAM: o ASR e o TTS rodam em outros processos e precisam da placa."""
    global _model, _state
    if _model is not None:
        del _model
        _model = None
        _state = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def _enhance_long(model, state, waveform: torch.Tensor, sample_rate: int) -> torch.Tensor:
    from df.enhance import enhance

    total = waveform.shape[-1]
    chunk = int(CHUNK_SECONDS * sample_rate)
    if total <= chunk:
        return enhance(model, state, waveform.contiguous())

    crossfade = int(CROSSFADE_SECONDS * sample_rate)
    step = chunk - crossfade
    fade_in = torch.linspace(0.0, 1.0, crossfade)
    fade_out = 1.0 - fade_in
    cleaned = torch.zeros_like(waveform)
    start = 0
    while start < total:
        end = min(start + chunk, total)
        # .contiguous() importa: o cuDNN recusa entrada nao contigua.
        piece = enhance(model, state, waveform[..., start:end].contiguous())
        piece = piece[..., : end - start]
        if start == 0:
            cleaned[..., start:end] = piece
        else:
            emenda = min(crossfade, piece.shape[-1])
            cleaned[..., start : start + emenda] = (
                cleaned[..., start : start + emenda] * fade_out[:emenda]
                + piece[..., :emenda] * fade_in[:emenda]
            )
            cleaned[..., start + emenda : end] = piece[..., emenda:]
        if end >= total:
            break
        start += step
    return cleaned


def clean_original(job_id: str) -> Path:
    """Limpa o mix original com DeepFilterNet antes da traducao S2ST.

    O arquivo limpo e mono 48 kHz (taxa nativa do DeepFilterNet3); o VAD/Seamless
    fazem a conversao para 16 kHz no passo seguinte. O instrumental separado segue
    intacto para a mixagem final.
    """
    output = jobs.cleaned_original_path(job_id)
    if output.exists():
        return output

    waveform, sample_rate = media.read_wav(jobs.raw_audio_path(job_id))
    waveform = waveform.mean(0, keepdim=True)
    model, state = _get_model()
    target_sample_rate = int(state.sr())
    if sample_rate != target_sample_rate:
        waveform = resample(waveform, sample_rate, target_sample_rate)

    cleaned = _enhance_long(model, state, waveform, target_sample_rate)
    media.write_wav(output, cleaned, target_sample_rate)
    return output
