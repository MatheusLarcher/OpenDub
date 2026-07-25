"""Transcricao com Parakeet TDT 0.6B v3, usada em duas frentes.

1. Transcrever a fala original (ingles) com tempo por token, base da traducao.
2. Conferir o que o TTS realmente falou: geramos, transcrevemos de volta e comparamos
   com o texto pedido. Foi assim que pegamos tomadas corrompidas que tinham duracao
   plausivel ("...eu vou te contar porque eu estimular") e que nenhum check de
   tamanho detectaria.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Dict, List

from backend.services import jobs, subprocess_env

ASR_PYTHON_VAR = "OPENDUB_ASR_PYTHON"
NOME_AMIGAVEL = "reconhecimento de fala"


def _normalize(text: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() or c == " " else " " for c in text).split())


def similarity(expected: str, actual: str) -> float:
    """Quanto o audio falado bate com o texto pedido (0 a 1)."""
    return difflib.SequenceMatcher(None, _normalize(expected), _normalize(actual)).ratio()


def transcribe_files(job_id: str, files: List[Dict], tag: str) -> Dict[str, Dict]:
    """files: [{"id": str, "path": str}] -> {id: {"text", "tokens"}}"""
    if not files:
        return {}
    response = subprocess_env.run_worker(
        ASR_PYTHON_VAR,
        NOME_AMIGAVEL,
        "asr_worker.py",
        {"files": files},
        jobs.job_dir(job_id) / "workers",
        tag,
    )
    return {item["id"]: item for item in response["results"]}


def transcribe_segments(job_id: str, segment_paths: List[Path]) -> List[Dict]:
    """Transcreve os blocos de fala do video original, na ordem recebida."""
    files = [{"id": str(index), "path": str(path)} for index, path in enumerate(segment_paths)]
    results = transcribe_files(job_id, files, "asr_origem")
    saida: List[Dict] = []
    for index in range(len(segment_paths)):
        item = results.get(str(index), {})
        if item.get("error"):
            print(f"[dub] bloco {index + 1}: transcricao falhou ({item['error']})")
        saida.append({"text": item.get("text", ""), "tokens": item.get("tokens", [])})
    return saida
