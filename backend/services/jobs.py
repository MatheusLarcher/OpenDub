from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List
from uuid import uuid4

from fastapi import HTTPException

BASE_DIR = Path(__file__).resolve().parent.parent
# No aplicativo empacotado, os fontes ficam em resources (somente leitura).
# O Electron define OPENDUB_DATA_DIR para que jobs, downloads e modelos gerados
# fiquem no perfil do usuario, fora do instalador.
DATA_DIR = Path(os.environ["OPENDUB_DATA_DIR"]).resolve() if os.getenv("OPENDUB_DATA_DIR") else BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
JOBS_DIR = DATA_DIR / "jobs"


def ensure_dirs() -> None:
    for folder in (DATA_DIR, UPLOAD_DIR, JOBS_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def create_job() -> tuple[str, Path]:
    ensure_dirs()
    job_id = uuid4().hex
    job_folder = JOBS_DIR / job_id
    job_folder.mkdir(parents=True, exist_ok=True)
    return job_id, job_folder


def job_dir(job_id: str) -> Path:
    path = JOBS_DIR / job_id
    if not path.exists():
        raise HTTPException(status_code=404, detail="Job nao encontrado")
    return path


def job_meta_path(job_id: str) -> Path:
    return job_dir(job_id) / "job.json"


def raw_audio_path(job_id: str) -> Path:
    return job_dir(job_id) / "raw_44k_stereo.wav"


def cleaned_original_path(job_id: str) -> Path:
    return job_dir(job_id) / "cleaned_original_48k_mono.wav"


def vocals_path(job_id: str) -> Path:
    return job_dir(job_id) / "vocals.wav"


def vocals_16k_path(job_id: str) -> Path:
    return job_dir(job_id) / "vocals_16k_mono.wav"


def instrumental_path(job_id: str) -> Path:
    return job_dir(job_id) / "instrumental.wav"


def dub_segments_path(job_id: str) -> Path:
    return job_dir(job_id) / "dub_segments.json"


def dubbed_audio_path(job_id: str) -> Path:
    return job_dir(job_id) / "dubbed.wav"


def seamless_audio_path(job_id: str) -> Path:
    """Audio direto do Seamless antes da conversao para a voz de referencia."""
    return job_dir(job_id) / "dubbed_seamless.wav"


def voice_reference_path(job_id: str) -> Path:
    """Trecho vocal limpo do proprio video usado pelo Seed-VC como referencia."""
    return job_dir(job_id) / "voice_reference.wav"


def dubbed_video_path(job_id: str) -> Path:
    return job_dir(job_id) / "dubbed.mp4"


def transcription_path(job_id: str) -> Path:
    return job_dir(job_id) / "transcription.json"


def subtitles_srt_path(job_id: str) -> Path:
    return job_dir(job_id) / "subtitles.srt"


def transcript_txt_path(job_id: str) -> Path:
    return job_dir(job_id) / "transcript.txt"


def save_json(path: Path, data: List[Dict] | Dict) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def load_json(path: Path) -> List[Dict] | Dict:
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Arquivo nao encontrado: {path.name}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_job_meta(job_id: str, data: Dict) -> None:
    save_json(job_meta_path(job_id), data)


def load_job_meta(job_id: str) -> Dict:
    path = job_meta_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Metadados do job nao encontrados")
    return load_json(path)


def resolve_media_path(job_id: str) -> Path:
    meta = load_job_meta(job_id)
    media_path = meta.get("media_path")
    if not media_path:
        raise HTTPException(status_code=404, detail="Media path nao encontrado")
    path = Path(media_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Arquivo de midia nao encontrado")
    return path
