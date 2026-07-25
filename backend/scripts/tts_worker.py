"""Gera a voz em portugues com o Chatterbox Multilingual V3 (pack pt-BR).

Roda em ambiente proprio: o Chatterbox fixa transformers 5.2.0, incompativel com o
que o Parakeet TDT exige. Recebe os blocos por JSON e escreve um wav por bloco.

Dois cuidados aprendidos testando:
- o texto precisa vir acentuado corretamente (o modelo pt-BR e baseado em grafemas);
  sem acento ele emudece ou corta a frase;
- o modelo as vezes colapsa e devolve quase silencio, entao cada bloco pode ser
  regerado com outra seed. Quem confere o conteudo e o ASR, do lado do backend.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from chatterbox.models.s3gen import S3Gen
from chatterbox.models.t3 import T3
from chatterbox.models.t3.modules.t3_config import T3Config
from chatterbox.models.tokenizers import MTLTokenizer
from chatterbox.models.voice_encoder import VoiceEncoder
from chatterbox.mtl_tts import ChatterboxMultilingualTTS, Conditionals
from huggingface_hub import hf_hub_download
from safetensors.torch import load_file as load_safetensors

BASE_REPO = "ResembleAI/chatterbox"
PTBR_REPO = "ResembleAI/Chatterbox-Multilingual-pt-br"
# Em texto longo o modelo perde o fio e devolve frase corrompida (medido: 3% de
# fidelidade com 133 caracteres). Fatiar por frase e concatenar resolve.
MAX_CHARS = 120
# Emenda curta entre as partes, para nao soar cortado.
JOIN_SILENCE_S = 0.12
# Buffers derivados que o checkpoint V3 nao carrega e o proprio modelo recalcula.
# Carregar com strict=False preserva o V3; trocar pelo s3gen.pt antigo rebaixaria o modelo.
RECOMPUTED_BUFFERS = {"tokenizer._mel_filters", "tokenizer.window"}


def _download(repo: str, filename: str) -> Path:
    return Path(hf_hub_download(repo_id=repo, filename=filename))


def _split_words(texto: str, limite: int) -> list[str]:
    partes: list[str] = []
    atual = ""
    for palavra in texto.split():
        candidato = f"{atual} {palavra}".strip()
        if len(candidato) <= limite:
            atual = candidato
        else:
            if atual:
                partes.append(atual)
            atual = palavra
    if atual:
        partes.append(atual)
    return partes


def split_text(texto: str, limite: int = MAX_CHARS) -> list[str]:
    """Quebra em frases de no maximo ``limite`` caracteres, sem cortar palavra."""
    normalizado = " ".join(texto.split())
    if len(normalizado) <= limite:
        return [normalizado]
    partes: list[str] = []
    for frase in re.split(r"(?<=[.!?:;])\s+", normalizado):
        frase = frase.strip()
        if frase:
            partes.extend(_split_words(frase, limite))
    return partes or [normalizado]


def load_model(device: str = "cuda") -> ChatterboxMultilingualTTS:
    voice_encoder = VoiceEncoder()
    voice_encoder.load_state_dict(
        torch.load(_download(BASE_REPO, "ve.pt"), map_location="cpu", weights_only=True)
    )
    voice_encoder.to(device).eval()

    t3 = T3(T3Config.multilingual())
    state = load_safetensors(_download(PTBR_REPO, "t3_pt_br.safetensors"))
    if "model" in state:
        state = state["model"][0]
    t3.load_state_dict(state)
    t3.to(device).eval()

    s3gen = S3Gen()
    incompatible = s3gen.load_state_dict(
        torch.load(_download(PTBR_REPO, "s3gen_v3.pt"), map_location="cpu", weights_only=True),
        strict=False,
    )
    if set(incompatible.missing_keys) != RECOMPUTED_BUFFERS or incompatible.unexpected_keys:
        raise RuntimeError(
            "checkpoint pt-BR incompativel: "
            f"missing={incompatible.missing_keys} unexpected={incompatible.unexpected_keys}"
        )
    s3gen.to(device).eval()

    return ChatterboxMultilingualTTS(
        t3,
        s3gen,
        voice_encoder,
        MTLTokenizer(str(_download(BASE_REPO, "grapheme_mtl_merged_expanded_v1.json"))),
        device,
        conds=Conditionals.load(_download(BASE_REPO, "conds.pt"), map_location="cpu").to(device),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    request = json.loads(Path(args.input).read_text(encoding="utf-8"))
    model = load_model()
    model.prepare_conditionals(request["reference"], exaggeration=request.get("exaggeration", 0.5))

    results = []
    for block in request["blocks"]:
        seed = int(block["seed"])
        try:
            partes = split_text(block["text"])
            trechos = []
            emenda = np.zeros(int(model.sr * JOIN_SILENCE_S), dtype=np.float32)
            for ordem, parte in enumerate(partes):
                torch.manual_seed(seed + ordem)
                torch.cuda.manual_seed_all(seed + ordem)
                wave = model.generate(
                    parte,
                    language_id="pt",
                    exaggeration=request.get("exaggeration", 0.5),
                    # cfg_weight menor acelera, mas em teste truncou a frase em 100% das
                    # tomadas (51% de fidelidade no ASR). Nao reduzir sem medir de novo.
                    cfg_weight=0.5,
                    temperature=0.8,
                    repetition_penalty=1.2,
                    min_p=0.05,
                    top_p=1.0,
                )
                if trechos:
                    trechos.append(emenda)
                trechos.append(wave.squeeze().float().cpu().numpy())
            audio = np.concatenate(trechos) if trechos else np.zeros(1, dtype=np.float32)
            sf.write(block["output"], audio, model.sr)
            results.append(
                {
                    "id": block["id"],
                    "path": block["output"],
                    "duration_s": len(audio) / model.sr,
                    "sample_rate": model.sr,
                }
            )
        except Exception as exc:  # pragma: no cover - depende de GPU/modelo
            results.append({"id": block["id"], "error": f"{type(exc).__name__}: {exc}"})

    Path(args.output).write_text(
        json.dumps({"results": results, "sample_rate": model.sr}, ensure_ascii=False),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
