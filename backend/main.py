from __future__ import annotations

import sys
from threading import Lock
from pathlib import Path
from typing import Literal

BASE_DIR = Path(__file__).resolve().parent
# Rodar `python backend/main.py` diretamente (fora do uvicorn com app_dir) nao coloca a
# raiz do repo no sys.path -- sem isso, `from backend.services import ...` falha no boot.
if str(BASE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(BASE_DIR.parent))

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from backend.services import jobs

load_dotenv(BASE_DIR / ".env")

app = FastAPI(title="OpenDub")

# Os modelos de separacao, VAD, DeepFilterNet e SeamlessM4T sao mantidos em memoria
# pelo processo. Eles nao podem ser carregados/descarregados por duas requisicoes ao
# mesmo tempo: alem de duplicar VRAM, isso pode derrubar o worker do Uvicorn.
_dub_lock = Lock()
_active_dub_job_id: str | None = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"]
)


class YoutubeRequest(BaseModel):
    url: str


class JobRequest(BaseModel):
    job_id: str
    model_input: Literal["vocals", "original", "deepfilter_original"] = "deepfilter_original"
    preserve_original_voice: bool = False


class SubtitlesRequest(BaseModel):
    job_id: str
    confirm: bool = False


@app.post("/process/youtube")
def process_youtube(payload: YoutubeRequest):
    job_id, job_folder = jobs.create_job()
    try:
        from backend.baixar_youtube import baixar_video
    except ModuleNotFoundError as exc:
        if exc.name not in {"backend", "backend.baixar_youtube"}:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Falha ao importar backend/baixar_youtube.py. "
                    f"Dependencia ausente: {exc.name}. Instale o requirements.txt."
                )
            ) from exc
        try:
            from backend.scripts.baixar_youtube import baixar_video
        except Exception as exc2:  # pragma: no cover
            raise HTTPException(
                status_code=500,
                detail=(
                    "Script baixar_youtube.py nao encontrado. Coloque o arquivo em "
                    "backend/baixar_youtube.py ou backend/scripts/baixar_youtube.py"
                )
            ) from exc2
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Falha ao importar backend/baixar_youtube.py. "
                f"Erro: {exc}"
            )
        ) from exc
    from yt_dlp.utils import YoutubeDLError

    try:
        result = baixar_video([payload.url], str(job_folder))
    except YoutubeDLError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Nao foi possivel baixar esse video do YouTube agora (o YouTube "
                "recusou o pedido). Tente novamente em alguns minutos ou baixe o "
                "video manualmente e envie o arquivo pelo botao de upload."
            )
        ) from exc
    if isinstance(result, (list, tuple)) and result:
        media_file = Path(result[0])
    else:
        media_file = Path(result)
    if not media_file.exists():
        raise HTTPException(status_code=400, detail="Download do YouTube falhou")
    if media_file.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"}:
        raise HTTPException(
            status_code=400,
            detail="Arquivo baixado nao contem video. Verifique o link e o formato."
        )
    jobs.save_job_meta(job_id, {"media_path": str(media_file), "source_type": "youtube"})
    return {"job_id": job_id, "media_path": str(media_file), "source_type": "youtube"}


@app.post("/process/upload")
async def process_upload(file: UploadFile = File(...)):
    job_id, job_folder = jobs.create_job()
    suffix = Path(file.filename).suffix or ".mp4"
    target_path = job_folder / f"upload{suffix}"
    with target_path.open("wb") as handle:
        import shutil
        shutil.copyfileobj(file.file, handle)
    jobs.save_job_meta(job_id, {"media_path": str(target_path), "source_type": "upload"})
    return {"job_id": job_id, "media_path": str(target_path), "source_type": "upload"}


@app.post("/dub")
def dub(payload: JobRequest):
    global _active_dub_job_id
    from backend.services import dubbing_pipeline

    # Depois de um refresh, o navegador pode pedir para continuar o fluxo. Reusar
    # o audio/segmentos ja finalizados evita traduzir o video todo novamente.
    if jobs.dubbed_audio_path(payload.job_id).exists() and jobs.dub_segments_path(payload.job_id).exists():
        return {
            "job_id": payload.job_id,
            "segments": jobs.load_json(jobs.dub_segments_path(payload.job_id)),
            "audio_url": f"/export/audio/{payload.job_id}",
        }
    if not _dub_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="Ja existe uma dublagem em andamento nesta maquina. Aguarde ela terminar antes de iniciar outra.",
        )
    try:
        _active_dub_job_id = payload.job_id
        segments = dubbing_pipeline.run_dub(
            payload.job_id,
            model_input=payload.model_input,
            preserve_original_voice=payload.preserve_original_voice,
        )
    finally:
        _active_dub_job_id = None
        _dub_lock.release()
    return {"job_id": payload.job_id, "segments": segments, "audio_url": f"/export/audio/{payload.job_id}"}


@app.get("/jobs/{job_id}/status")
def job_status(job_id: str):
    """Estado recuperavel de um job; usado pela pagina depois de F5/reabertura."""
    global _active_dub_job_id
    meta = jobs.load_job_meta(job_id)
    media_exists = jobs.resolve_media_path(job_id).exists()
    segments_path = jobs.dub_segments_path(job_id)
    segments = jobs.load_json(segments_path) if segments_path.exists() else []
    return {
        "job_id": job_id,
        "media_ready": media_exists,
        "dub_ready": jobs.dubbed_audio_path(job_id).exists(),
        "video_ready": jobs.dubbed_video_path(job_id).exists(),
        "subtitles_ready": jobs.subtitles_srt_path(job_id).exists(),
        "processing_dub": _active_dub_job_id == job_id,
        "source_type": meta.get("source_type"),
        "segments": segments,
    }


@app.post("/generate-video")
def generate_video(payload: JobRequest):
    from backend.services import video

    video.mux_dubbed_video(payload.job_id)
    return {"job_id": payload.job_id, "video_url": f"/export/video/{payload.job_id}"}


@app.post("/subtitles/generate")
def generate_subtitles(payload: SubtitlesRequest):
    if not payload.confirm:
        raise HTTPException(
            status_code=400,
            detail="Confirme o download do modelo de legenda (confirm=true) antes de gerar."
        )
    from backend.services import subtitles

    segments = subtitles.generate_subtitles(payload.job_id)
    return {
        "job_id": payload.job_id,
        "segments": segments,
        "transcription_url": f"/export/transcription/{payload.job_id}",
        "subtitles_url": f"/export/subtitles/{payload.job_id}",
    }


@app.get("/export/transcription/{job_id}")
def export_transcription(job_id: str):
    path = jobs.transcription_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Legenda ainda nao gerada")
    return FileResponse(path, media_type="application/json", filename="transcription.json")


@app.get("/export/subtitles/{job_id}")
def export_subtitles(job_id: str):
    path = jobs.subtitles_srt_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Legenda ainda nao gerada")
    return FileResponse(path, media_type="text/plain", filename="subtitles.srt")


@app.get("/export/transcript-txt/{job_id}")
def export_transcript_txt(job_id: str):
    path = jobs.transcript_txt_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Legenda ainda nao gerada")
    return FileResponse(path, media_type="text/plain", filename="transcript.txt")


@app.get("/export/audio/{job_id}")
def export_audio(job_id: str):
    path = jobs.dubbed_audio_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Audio dublado ainda nao gerado")
    return FileResponse(path, media_type="audio/wav", filename="dubbed.wav")


@app.get("/export/video/{job_id}")
def export_video(job_id: str):
    path = jobs.dubbed_video_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Video dublado ainda nao gerado")
    return FileResponse(path, media_type="video/mp4", filename="dubbed.mp4")


@app.get("/export/original/{job_id}")
def export_original_video(job_id: str):
    path = jobs.resolve_media_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Video original nao encontrado")
    return FileResponse(path, media_type="video/mp4", filename="original.mp4")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=5501,
        reload=True,
        app_dir=str(BASE_DIR.parent)
    )
