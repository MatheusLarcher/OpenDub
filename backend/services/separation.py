from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

import torch
from torchaudio.pipelines import HDEMUCS_HIGH_MUSDB_PLUS
from torchaudio.transforms import Fade
from torchaudio.functional import resample

from backend.services import jobs, media
from backend.services._retry import with_retry

_bundle = HDEMUCS_HIGH_MUSDB_PLUS
_model: Optional[torch.nn.Module] = None


def _device() -> torch.device:
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def get_model() -> torch.nn.Module:
    global _model
    if _model is None:
        _model = with_retry(lambda: _bundle.get_model().to(_device()))
        _model.eval()
    return _model


def unload_model() -> None:
    global _model
    if _model is not None:
        del _model
        _model = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def _separate_sources(
    model: torch.nn.Module,
    mix: torch.Tensor,
    sample_rate: int,
    segment: float = 10.0,
    overlap: float = 0.1,
    device: Optional[torch.device] = None,
) -> torch.Tensor:
    """Aplica o modelo em blocos com fade/overlap-add (evita estourar VRAM em audios longos)."""
    if device is None:
        device = mix.device
    else:
        device = torch.device(device)

    batch, channels, length = mix.shape

    chunk_len = int(sample_rate * segment * (1 + overlap))
    start = 0
    end = chunk_len
    overlap_frames = int(overlap * sample_rate)
    fade = Fade(fade_in_len=0, fade_out_len=overlap_frames, fade_shape="linear")

    final = torch.zeros(batch, len(model.sources), channels, length, device=device)

    while start < length - overlap_frames:
        chunk = mix[:, :, start:end]
        with torch.no_grad():
            out = model.forward(chunk)
        out = fade(out)
        final[:, :, :, start:end] += out
        if start == 0:
            fade.fade_in_len = overlap_frames
            start += chunk_len - overlap_frames
        else:
            start += chunk_len
        end += chunk_len
        if end >= length:
            fade.fade_out_len = 0
    return final


def _peak_normalize(wav: torch.Tensor, headroom: float = 0.98) -> torch.Tensor:
    peak = wav.abs().max()
    if peak > headroom:
        wav = wav * (headroom / peak)
    return wav


def separate(job_id: str) -> Tuple[Path, Path]:
    """Separa vocal x instrumental da faixa original do job. Retorna (vocals_path, instrumental_path)."""
    vocals_out = jobs.vocals_path(job_id)
    instrumental_out = jobs.instrumental_path(job_id)
    if vocals_out.exists() and instrumental_out.exists():
        return vocals_out, instrumental_out

    raw_audio = media.extract_audio(job_id)
    device = _device()
    model = get_model()

    waveform, sample_rate = media.read_wav(raw_audio)
    if sample_rate != _bundle.sample_rate:
        waveform = resample(waveform, sample_rate, _bundle.sample_rate)
        sample_rate = _bundle.sample_rate
    waveform = waveform.to(device)

    ref = waveform.mean(0)
    ref_mean = ref.mean()
    ref_std = ref.std()
    normalized = (waveform - ref_mean) / ref_std

    sources = _separate_sources(
        model, normalized[None], sample_rate=sample_rate, device=device
    )[0]
    sources = sources * ref_std + ref_mean

    audios = dict(zip(model.sources, sources))
    vocals = audios["vocals"].cpu()
    instrumental = (audios["drums"] + audios["bass"] + audios["other"]).cpu()
    instrumental = _peak_normalize(instrumental)

    media.write_wav(vocals_out, vocals, sample_rate)
    media.write_wav(instrumental_out, instrumental, sample_rate)
    return vocals_out, instrumental_out
