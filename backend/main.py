from __future__ import annotations

import shutil
import sys
from threading import Lock
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

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


VIDEO_SUFFIXES = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg",
    ".wmv", ".flv", ".ts", ".m2ts", ".3gp", ".ogv",
}


def _discard_job_folder(job_folder: Path) -> None:
    """Um job que nunca chegou a ter midia nao deve deixar pasta para tras."""
    shutil.rmtree(job_folder, ignore_errors=True)


@app.post("/process/youtube")
def process_youtube(payload: YoutubeRequest):
    parsed = urlparse(payload.url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail=(
                "Esse link nao parece ser de um video. Copie o endereco completo do "
                "video (comecando com https://) e cole aqui."
            )
        )
    job_id, job_folder = jobs.create_job()
    try:
        try:
            from backend.baixar_youtube import baixar_video
        except ModuleNotFoundError as exc:
            if exc.name in {"backend", "backend.baixar_youtube"}:
                from backend.scripts.baixar_youtube import baixar_video
            else:
                raise
        from yt_dlp.utils import DownloadError, YoutubeDLError

        try:
            result = baixar_video([payload.url], str(job_folder))
        except YoutubeDLError as exc:
            # Nem toda falha e culpa do YouTube: link invalido, video privado ou removido
            # tem causa e solucao diferentes, e mandar "tente em alguns minutos" nesses
            # casos so faz o usuario repetir algo que nunca vai funcionar.
            message = str(exc).lower()
            if isinstance(exc, DownloadError) and (
                "unsupported url" in message or "is not a valid url" in message
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Esse link nao e de um video que sabemos baixar. Use o endereco "
                        "de um video do YouTube ou envie o arquivo pelo botao de upload."
                    )
                ) from exc
            if any(
                marker in message
                for marker in ("private video", "video unavailable", "removed", "does not exist")
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Esse video nao esta disponivel (pode ser privado, restrito ou ter "
                        "sido removido). Tente outro link ou envie o arquivo pelo upload."
                    )
                ) from exc
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
            raise HTTPException(
                status_code=400,
                detail="O download desse video nao foi concluido. Tente novamente."
            )
        if media_file.suffix.lower() not in VIDEO_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail="O arquivo baixado nao contem video. Confira o link e tente de novo."
            )
    except HTTPException:
        _discard_job_folder(job_folder)
        raise
    except Exception as exc:
        _discard_job_folder(job_folder)
        print(f"[opendub] falha inesperada ao baixar do YouTube: {exc!r}", file=sys.stderr)
        raise HTTPException(
            status_code=500,
            detail="Nao foi possivel preparar esse video. Tente novamente."
        ) from exc
    jobs.save_job_meta(job_id, {"media_path": str(media_file), "source_type": "youtube"})
    return {"job_id": job_id, "media_path": str(media_file), "source_type": "youtube"}


@app.post("/process/upload")
async def process_upload(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    # Antes qualquer arquivo era aceito e a falha só aparecia no meio da dublagem, com uma
    # mensagem crua do ffmpeg. Melhor recusar na hora, com o motivo em portugues.
    if suffix not in VIDEO_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=(
                "Esse arquivo nao e um video que sabemos abrir. Envie um video "
                "(mp4, mov, mkv, webm, avi...)."
            )
        )
    job_id, job_folder = jobs.create_job()
    target_path = job_folder / f"upload{suffix}"
    try:
        with target_path.open("wb") as handle:
            shutil.copyfileobj(file.file, handle)
    except Exception as exc:
        _discard_job_folder(job_folder)
        print(f"[opendub] falha ao gravar o upload: {exc!r}", file=sys.stderr)
        raise HTTPException(
            status_code=500,
            detail="Nao foi possivel salvar esse video. Confira o espaco em disco e tente de novo."
        ) from exc
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
    # Nao usar resolve_media_path aqui: ela levanta 404 quando o arquivo original saiu do
    # lugar, e o front trata 404 como "esse job nao existe mais" e apaga a sessao -- o
    # usuario perdia o acesso ao video ja dublado por causa do arquivo de origem.
    media_path = meta.get("media_path")
    media_exists = bool(media_path) and Path(media_path).exists()
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
