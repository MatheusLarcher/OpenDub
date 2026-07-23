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

    from df.enhance import enhance

    cleaned = enhance(model, state, waveform)
    media.write_wav(output, cleaned, target_sample_rate)
    return output
