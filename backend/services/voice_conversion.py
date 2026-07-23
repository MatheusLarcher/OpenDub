from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List

import torch
from fastapi import HTTPException
from torchaudio.functional import resample

from backend.services import jobs, media, vad

SEED_VC_DIR = Path(
    os.getenv("SEED_VC_DIR", str(Path.home() / ".codex" / "cache" / "seed-vc"))
)
SEED_VC_CONDA_ENV = os.getenv("SEED_VC_CONDA_ENV", "seedvc")
SEED_VC_PYTHON = os.getenv("SEED_VC_PYTHON")
SEED_VC_STEPS = int(os.getenv("SEED_VC_STEPS", "30"))
REFERENCE_MAX_SECONDS = 15.0
REFERENCE_MIN_SECONDS = 2.0


def _seed_python() -> Path:
    """Localiza o Python do ambiente Seed-VC sem depender de `conda activate`.

    No Windows, `conda run` pode falhar antes de iniciar o Python quando o
    processo da API nao herdou a inicializacao do shell Conda. Chamar o
    executavel do ambiente diretamente evita esse problema.
    """
    if SEED_VC_PYTHON:
        candidate = Path(SEED_VC_PYTHON)
        if candidate.is_file():
            return candidate
        raise HTTPException(status_code=500, detail="SEED_VC_PYTHON nao aponta para um python.exe valido.")

    conda = shutil.which("conda")
    candidates: list[Path] = []
    if conda:
        conda_path = Path(conda).resolve()
        # Instalacao padrao do Miniconda: <raiz>\\condabin\\conda.bat.
        candidates.append(conda_path.parent.parent / "envs" / SEED_VC_CONDA_ENV / "python.exe")
    candidates.append(Path.home() / "miniconda3" / "envs" / SEED_VC_CONDA_ENV / "python.exe")
    candidates.append(Path.home() / "anaconda3" / "envs" / SEED_VC_CONDA_ENV / "python.exe")

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise HTTPException(
        status_code=500,
        detail=(
            "Python do ambiente Seed-VC nao encontrado. Defina SEED_VC_PYTHON "
            "no backend/.env (ex.: C:\\Users\\...\\envs\\seedvc\\python.exe)."
        ),
    )


def _seed_command(source: Path, reference: Path, output_dir: Path) -> List[str]:
    if not SEED_VC_DIR.is_dir() or not (SEED_VC_DIR / "inference.py").exists():
        raise HTTPException(
            status_code=500,
            detail=(
                "Conversao para manter a voz original nao esta configurada. "
                "Defina SEED_VC_DIR no backend/.env apontando para o Seed-VC."
            ),
        )
    return [
        str(_seed_python()), str(Path(__file__).resolve().parent.parent / "scripts" / "seed_vc_inference.py"),
        "--seed-vc-dir", str(SEED_VC_DIR), "--source", str(source), "--target", str(reference),
        "--output", str(output_dir), "--diffusion-steps", str(SEED_VC_STEPS),
        "--fp16", "true",
    ]


def _write_reference(job_id: str, chunks: List[vad.Chunk]) -> Path:
    candidates = [chunk for chunk in chunks if chunk["end"] - chunk["start"] >= REFERENCE_MIN_SECONDS]
    if not candidates:
        raise HTTPException(
            status_code=422,
            detail="Nao foi encontrado trecho de fala suficiente para manter a voz original.",
        )
    best = max(candidates, key=lambda chunk: chunk["end"] - chunk["start"])
    end = min(best["end"], best["start"] + REFERENCE_MAX_SECONDS)
    vocals = vad.load_16k_mono(jobs.vocals_path(job_id))
    reference = vad.slice_audio(vocals, best["start"], end)
    path = jobs.voice_reference_path(job_id)
    media.write_wav(path, reference[None, :], vad.VAD_SAMPLE_RATE)
    return path


def convert_to_original_voice(job_id: str, source: Path, chunks: List[vad.Chunk], output_sample_rate: int) -> Path:
    """Converte o timbre do Seamless para a voz do video sem mudar texto/tempos."""
    reference = _write_reference(job_id, chunks)
    output_dir = jobs.job_dir(job_id) / "seed_vc_output"
    output_dir.mkdir(exist_ok=True)
    for old_output in output_dir.glob("*.wav"):
        old_output.unlink()
    log_path = jobs.job_dir(job_id) / "seed_vc.log"
    try:
        result = subprocess.run(
            _seed_command(source, reference, output_dir),
            cwd=SEED_VC_DIR,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        log_path.write_text((result.stdout or "") + (result.stderr or ""), encoding="utf-8")
    except subprocess.CalledProcessError as exc:
        details = ((exc.stdout or "") + (exc.stderr or "")).strip()
        log_path.write_text(details, encoding="utf-8")
        last_line = next((line.strip() for line in reversed(details.splitlines()) if line.strip()), "sem detalhe retornado")
        raise HTTPException(
            status_code=500,
            detail=f"Seed-VC falhou ao converter a voz original: {last_line[:300]}",
        ) from exc
    generated = list(output_dir.glob("*.wav"))
    if len(generated) != 1:
        raise HTTPException(status_code=500, detail="Seed-VC nao gerou o audio convertido esperado.")
    wave, sample_rate = media.read_wav(generated[0])
    if wave.shape[0] > 1:
        wave = wave.mean(dim=0, keepdim=True)
    if sample_rate != output_sample_rate:
        wave = resample(wave, sample_rate, output_sample_rate)
    source_wave, _ = media.read_wav(source)
    target_samples = source_wave.shape[-1]
    if wave.shape[-1] < target_samples:
        wave = torch.nn.functional.pad(wave, (0, target_samples - wave.shape[-1]))
    else:
        wave = wave[:, :target_samples]
    destination = jobs.dubbed_audio_path(job_id)
    media.write_wav(destination, wave, output_sample_rate)
    return destination
