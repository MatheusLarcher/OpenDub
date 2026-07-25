"""Executa um worker em outro ambiente Python.

ASR (Parakeet) e TTS (Chatterbox) exigem versoes diferentes e incompativeis do
transformers, entao cada um vive em um venv proprio criado pelo bootstrap do app.
Chamamos o python.exe do ambiente diretamente: no Windows, `conda run` costuma
falhar antes de iniciar o Python quando o processo da API nao herdou a
inicializacao do shell.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Dict

from fastapi import HTTPException

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _python_for(variable: str, nome_amigavel: str) -> Path:
    configured = os.getenv(variable)
    if not configured:
        raise HTTPException(
            status_code=500,
            detail=f"O componente de {nome_amigavel} nao foi preparado. Reabra o aplicativo.",
        )
    candidate = Path(configured)
    if not candidate.is_file():
        raise HTTPException(
            status_code=500,
            detail=f"O componente de {nome_amigavel} nao foi encontrado. Reabra o aplicativo.",
        )
    return candidate


def run_worker(
    variable: str,
    nome_amigavel: str,
    script: str,
    payload: Dict,
    work_dir: Path,
    tag: str,
) -> Dict:
    """Escreve o pedido em JSON, roda o worker e devolve a resposta em JSON."""
    work_dir.mkdir(parents=True, exist_ok=True)
    input_path = work_dir / f"{tag}_in.json"
    output_path = work_dir / f"{tag}_out.json"
    log_path = work_dir / f"{tag}.log"
    input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    command = [
        str(_python_for(variable, nome_amigavel)),
        str(SCRIPTS_DIR / script),
        "--input",
        str(input_path),
        "--output",
        str(output_path),
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        log_path.write_text((result.stdout or "") + (result.stderr or ""), encoding="utf-8")
    except subprocess.CalledProcessError as exc:
        details = ((exc.stdout or "") + (exc.stderr or "")).strip()
        log_path.write_text(details, encoding="utf-8")
        last_line = next(
            (line.strip() for line in reversed(details.splitlines()) if line.strip()),
            "sem detalhe",
        )
        print(f"[opendub] worker {script} falhou: {last_line[:500]}")
        raise HTTPException(
            status_code=500,
            detail=f"Nao foi possivel concluir a etapa de {nome_amigavel}. Tente novamente.",
        ) from exc

    if not output_path.exists():
        raise HTTPException(
            status_code=500,
            detail=f"A etapa de {nome_amigavel} nao produziu resultado. Tente novamente.",
        )
    return json.loads(output_path.read_text(encoding="utf-8"))
