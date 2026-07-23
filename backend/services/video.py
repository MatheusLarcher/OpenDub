from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException

from backend.services import jobs, media

INSTRUMENTAL_GAIN_DB = float(os.getenv("INSTRUMENTAL_GAIN_DB", "-4"))


def mux_dubbed_video(job_id: str) -> Path:
    """Remonta o video final. O video NUNCA e retimado -- cortes, musica e mudancas de
    cena ficam exatamente onde estavam no original; quem se ajusta e so a voz dublada
    (ver dubbing_pipeline.run_dub), que ja chega posicionada nos timestamps certos.
    """
    media.ensure_ffmpeg_available()
    source = jobs.resolve_media_path(job_id)
    dubbed_audio = jobs.dubbed_audio_path(job_id)
    instrumental = jobs.instrumental_path(job_id)
    if not dubbed_audio.exists():
        raise HTTPException(status_code=404, detail="Audio dublado ainda nao gerado (rode /dub primeiro)")
    if not instrumental.exists():
        raise HTTPException(status_code=404, detail="Instrumental ainda nao gerado (rode /dub primeiro)")

    output = jobs.dubbed_video_path(job_id)
    filter_complex = (
        f"[2:a]volume={INSTRUMENTAL_GAIN_DB}dB[instr_gained];"
        "[instr_gained][1:a]amix=inputs=2:duration=first:normalize=0[a_out]"
    )
    command = [
        "ffmpeg",
        "-y",
        "-i", str(source),
        "-i", str(dubbed_audio),
        "-i", str(instrumental),
        "-filter_complex", filter_complex,
        "-map", "0:v:0",
        "-map", "[a_out]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-metadata:s:a:0", "title=Dublado",
        "-shortest",
        str(output),
    ]
    media.run_ffmpeg(command)
    return output
