"""Gera a voz dublada com o Chatterbox pt-BR e confere o resultado com o ASR.

O modelo as vezes colapsa: devolve quase silencio, ou uma frase truncada/corrompida
com duracao plausivel. Checar so a duracao nao pega o segundo caso -- em teste, uma
tomada de 6,8 s (esperado ~10 s) falou "eu vou te contar porque eu estimular".

Por isso cada tomada e transcrita de volta pelo Parakeet e comparada com o texto que
pedimos. Abaixo do limite de fidelidade, tentamos outra seed. Como o ASR ja faz parte
do pipeline, a conferencia sai barata.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List

from backend.services import asr, jobs, subprocess_env

TTS_PYTHON_VAR = "OPENDUB_TTS_PYTHON"
NOME_AMIGAVEL = "geracao de voz"
# Abaixo disso a tomada saiu truncada ou corrompida. Medido: tomadas boas ficam em
# 98% e as ruins em 51% ou 0%, entao 0.80 separa bem os dois grupos.
MIN_FIDELIDADE = float(os.getenv("OPENDUB_TTS_MIN_FIDELIDADE", "0.80"))
MAX_TENTATIVAS = max(1, int(os.getenv("OPENDUB_TTS_MAX_TENTATIVAS", "3")))
# Interjeicao ("Ei", "Ha") nao da material para o ASR comparar: "Ei" transcrito como
# "Ê" ja derruba a similaridade para 40% sem que o audio esteja errado. Nesses casos
# so exigimos que tenha saido som.
TEXTO_CURTO_CHARS = 12
MIN_DURACAO_S = 0.15


def _blocks_dir(job_id: str) -> Path:
    path = jobs.job_dir(job_id) / "tts_blocks"
    path.mkdir(exist_ok=True)
    return path


def _synthesize(job_id: str, reference: Path, pedidos: List[Dict], tag: str) -> Dict[str, Dict]:
    response = subprocess_env.run_worker(
        TTS_PYTHON_VAR,
        NOME_AMIGAVEL,
        "tts_worker.py",
        {"reference": str(reference), "blocks": pedidos},
        jobs.job_dir(job_id) / "workers",
        tag,
    )
    return {item["id"]: item for item in response["results"]}


def synthesize_blocks(job_id: str, reference: Path, textos: List[str]) -> List[Dict]:
    """Gera um wav por bloco, repetindo os que nao passarem na conferencia.

    Devolve, na ordem: {"path", "duration_s", "sample_rate", "fidelidade", "tentativas"}.
    Bloco sem texto (ou que falhou em todas as tentativas) vem com path None.
    """
    # fidelidade comeca negativa: uma tomada que sai com 0.0 (o modelo colapsou e o ASR
    # nao ouviu nada) ainda precisa ser guardada, senao o bloco vira silencio sem que
    # ninguem perceba -- o video sairia mudo naquele trecho e a legenda mostraria a frase.
    saida: List[Dict] = [
        {"path": None, "duration_s": 0.0, "sample_rate": 0, "fidelidade": -1.0, "tentativas": 0}
        for _ in textos
    ]
    pendentes = [index for index, texto in enumerate(textos) if texto.strip()]
    blocks_dir = _blocks_dir(job_id)

    for tentativa in range(MAX_TENTATIVAS):
        if not pendentes:
            break
        pedidos = [
            {
                "id": str(index),
                "text": textos[index],
                # Seed diferente por tentativa: o colapso do modelo depende da seed.
                "seed": 1000 * tentativa + index,
                "output": str(blocks_dir / f"bloco_{index:03d}_t{tentativa}.wav"),
            }
            for index in pendentes
        ]
        gerados = _synthesize(job_id, reference, pedidos, f"tts_t{tentativa}")

        conferir = []
        for index in pendentes:
            item = gerados.get(str(index), {})
            if item.get("error") or not item.get("path"):
                print(f"[dub] bloco {index + 1}: geracao falhou ({item.get('error', 'sem saida')})")
                continue
            if len(textos[index].strip()) > TEXTO_CURTO_CHARS:
                conferir.append({"id": str(index), "path": item["path"]})
        transcricoes = asr.transcribe_files(job_id, conferir, f"tts_check_t{tentativa}")

        ainda_pendentes: List[int] = []
        for index in pendentes:
            item = gerados.get(str(index), {})
            if item.get("error") or not item.get("path"):
                ainda_pendentes.append(index)
                continue
            if len(textos[index].strip()) <= TEXTO_CURTO_CHARS:
                fidelidade = 1.0 if item["duration_s"] >= MIN_DURACAO_S else 0.0
            else:
                falado = transcricoes.get(str(index), {}).get("text", "")
                fidelidade = asr.similarity(textos[index], falado)
            melhor = saida[index]
            if fidelidade > melhor["fidelidade"]:
                saida[index] = {
                    "path": item["path"],
                    "duration_s": item["duration_s"],
                    "sample_rate": item["sample_rate"],
                    "fidelidade": fidelidade,
                    "tentativas": tentativa + 1,
                }
            if fidelidade < MIN_FIDELIDADE:
                ainda_pendentes.append(index)
                print(
                    f"[dub] bloco {index + 1}: fidelidade {fidelidade:.0%} "
                    f"(minimo {MIN_FIDELIDADE:.0%}), refazendo"
                )
        pendentes = ainda_pendentes

    for index, item in enumerate(saida):
        if item["fidelidade"] < 0:
            item["fidelidade"] = 0.0
            if textos[index].strip():
                print(f"[dub] bloco {index + 1}: nenhuma tomada foi gerada; ficara em silencio")
        elif item["path"] and item["fidelidade"] < MIN_FIDELIDADE:
            print(
                f"[dub] bloco {index + 1}: melhor tomada ficou em {item['fidelidade']:.0%}; "
                "seguindo com ela"
            )
    return saida
