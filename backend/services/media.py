from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

import librosa
import numpy as np
import soundfile as sf
import torch
from fastapi import HTTPException

from backend.services import jobs


def ensure_ffmpeg_available() -> None:
    if shutil.which("ffmpeg") is None:
        raise HTTPException(
            status_code=500,
            detail="FFmpeg nao encontrado no PATH. Instale para gerar o video dublado."
        )


def run_ffmpeg(command: List[str]) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        # O erro tecnico vai para o log; o usuario recebe algo que ele consegue agir sobre.
        # Antes o texto cru do ffmpeg ("Invalid data found when processing input") subia
        # direto para a tela.
        if exc.stderr:
            print(f"[opendub] ffmpeg falhou: {exc.stderr.strip()}", file=sys.stderr)
        raise HTTPException(
            status_code=500,
            detail=(
                "Nao foi possivel processar esse video. O arquivo pode estar corrompido "
                "ou em um formato que nao conseguimos abrir."
            )
        ) from exc


def read_wav(path: Path) -> Tuple[torch.Tensor, int]:
    """Le um wav como tensor (channels, frames) via soundfile.

    Evita torchaudio.load: no torchaudio>=2.9 ele sempre usa o backend torchcodec, que por
    sua vez exige as DLLs compartilhadas do ffmpeg (nosso ffmpeg e o build "essentials",
    estatico, sem essas DLLs).
    """
    data, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    tensor = torch.from_numpy(np.ascontiguousarray(data.T))
    return tensor, sample_rate


def write_wav(path: Path, tensor: torch.Tensor, sample_rate: int) -> None:
    data = tensor.detach().cpu().numpy()
    if data.ndim == 1:
        data = data[None, :]
    sf.write(str(path), data.T, sample_rate)


def time_stretch(audio: np.ndarray, rate: float) -> np.ndarray:
    """Acelera/desacelera audio preservando o tom (phase vocoder). rate>1 = mais rapido."""
    return librosa.effects.time_stretch(audio, rate=rate)


def extract_audio(job_id: str) -> Path:
    """Extrai o audio original (44.1kHz estereo) do video/midia do job."""
    ensure_ffmpeg_available()
    output = jobs.raw_audio_path(job_id)
    if output.exists():
        return output
    source = jobs.resolve_media_path(job_id)
    command = [
        "ffmpeg",
        "-y",
        "-i", str(source),
        "-vn",
        "-ac", "2",
        "-ar", "44100",
        "-acodec", "pcm_s16le",
        str(output),
    ]
    run_ffmpeg(command)
    if not output.exists():
        raise HTTPException(status_code=500, detail="Falha ao extrair audio do video.")
    return output
