#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
from pathlib import Path
from typing import Iterable, List, Union
from yt_dlp import YoutubeDL

UrlLike = Union[str, Iterable[str]]

def _normalize_urls(urls: UrlLike) -> list[str]:
    if isinstance(urls, str):
        return [urls]
    return list(urls)

def baixar_audio(
    urls: UrlLike,
    pasta_saida: str = ".",
    codec: str | None = None,   # None = baixa .m4a sem converter (dispensa FFmpeg)
    bitrate_kbps: str = "192"   # usado só quando codec != None
) -> List[Path]:
    """
    Baixa somente o áudio de um ou vários vídeos/links (vídeo único, playlist ou canal).
    - Se `codec` for None: baixa o melhor áudio (preferindo .m4a) **sem** converter.
    - Se `codec` for 'mp3' | 'm4a' | 'opus' etc.: converte via FFmpeg (precisa FFmpeg no PATH).
    Retorna a lista de caminhos salvos.
    """
    pasta = Path(pasta_saida)
    pasta.mkdir(parents=True, exist_ok=True)

    # Saída: Título [ID].ext — evita nomes repetidos
    ydl_opts = {
        "outtmpl": str(pasta / "%(title)s [%(id)s].%(ext)s"),
        "quiet": False,
        "noprogress": False,
        # A aplicacao cria um job por video. Links do YouTube frequentemente trazem
        # `list`, `index` ou `start_radio`, mas nunca devemos expandir a playlist.
        "noplaylist": True,
    }

    if codec is None:
        # Sem FFmpeg: pega o melhor áudio já pronto (preferindo m4a)
        ydl_opts["format"] = "bestaudio[ext=m4a]/bestaudio/b"
    else:
        # Com FFmpeg: baixa o melhor e converte para o codec desejado
        ydl_opts.update({
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": codec,
                    "preferredquality": bitrate_kbps,
                }
            ],
        })

    arquivos: List[Path] = []
    with YoutubeDL(ydl_opts) as ydl:
        for url in _normalize_urls(urls):
            info = ydl.extract_info(url, download=True)
            arquivos.extend(_collect_filepaths(info))

    return arquivos


def baixar_video(
    urls: UrlLike,
    pasta_saida: str = ".",
    container: str = "mp4"
) -> List[Path]:
    """
    Baixa video + audio (melhor qualidade) e faz merge em um arquivo final.
    Requer FFmpeg no PATH para fazer o merge.
    """
    pasta = Path(pasta_saida)
    pasta.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "outtmpl": str(pasta / "%(title)s [%(id)s].%(ext)s"),
        "quiet": False,
        "noprogress": False,
        "noplaylist": True,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "merge_output_format": container
    }

    arquivos: List[Path] = []
    with YoutubeDL(ydl_opts) as ydl:
        for url in _normalize_urls(urls):
            info = ydl.extract_info(url, download=True)
            arquivos.extend(_collect_filepaths(info))

    return arquivos


def _collect_filepaths(info_dict) -> List[Path]:
    """Extrai os caminhos finais gerados pelo yt-dlp (inclui playlists)."""
    paths: List[Path] = []
    # Quando há pós-processamento, o yt-dlp preenche 'requested_downloads' com 'filepath'
    requested = info_dict.get("requested_downloads")
    if requested:
        for item in requested:
            fp = item.get("filepath")
            if fp:
                paths.append(Path(fp))
    elif "entries" in info_dict and info_dict["entries"]:
        for entry in info_dict["entries"]:
            paths.extend(_collect_filepaths(entry))
    else:
        # Fallback: pode não refletir mudança de extensão se houve conversão
        # mas cobre casos simples sem pós-processamento
        from yt_dlp import YoutubeDL
        with YoutubeDL() as ydl_tmp:
            paths.append(Path(ydl_tmp.prepare_filename(info_dict)))
    return paths


if __name__ == "__main__":
    # Uso:
    #   python baixar_audio.py "URL"               -> baixa 1 link, sem converter (m4a)
    #   python baixar_audio.py "URL" ./audios mp3 -> baixa e converte para mp3
    import sys
    if len(sys.argv) < 2:
        print("Uso: python baixar_audio.py <URL|playlist> [pasta_saida='.'] [codec=None|mp3|m4a|opus] [bitrate_kbps=192]")
        sys.exit(1)

    url = sys.argv[1]
    pasta = sys.argv[2] if len(sys.argv) > 2 else "."
    codec = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3].lower() != "none" else None
    bitrate = sys.argv[4] if len(sys.argv) > 4 else "192"

    saidas = baixar_audio(url, pasta_saida=pasta, codec=codec, bitrate_kbps=bitrate)
    for s in saidas:
        print("✔︎ Salvo:", s.resolve())
