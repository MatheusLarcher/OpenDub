"""Transcreve audio com o Parakeet TDT e devolve texto + tempos por token.

Roda em um ambiente proprio porque o Parakeet TDT exige transformers >= 5.10, versao
incompativel com a que o Chatterbox fixa. A comunicacao com o backend e por arquivo
JSON, para nao depender de nada instalado dos dois lados.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from transformers import AutoModel, AutoProcessor

MODEL_ID = "nvidia/parakeet-tdt-0.6b-v3"
SAMPLE_RATE = 16000

_model = None
_processor = None


def _device() -> str:
    if not torch.cuda.is_available():
        # Sem GPU o modelo ate carrega, mas leva tempo demais para um app de desktop.
        # Falhar aqui com texto claro e melhor do que estourar la dentro do CUDA.
        raise RuntimeError(
            "Este computador nao tem uma placa de video NVIDIA compativel, "
            "necessaria para dublar."
        )
    return "cuda"


def _load():
    global _model, _processor
    if _model is None:
        device = _device()
        _processor = AutoProcessor.from_pretrained(MODEL_ID)
        _model = AutoModel.from_pretrained(MODEL_ID, dtype=torch.float16).to(device).eval()
    return _processor, _model


def _read_mono_16k(path: Path) -> np.ndarray:
    audio, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if sample_rate != SAMPLE_RATE:
        import librosa

        audio = librosa.resample(audio, orig_sr=sample_rate, target_sr=SAMPLE_RATE)
    return audio


def transcribe(path: Path) -> dict:
    audio = _read_mono_16k(path)
    # Audio curto demais nao tem o que transcrever e faz o modelo devolver lixo.
    if len(audio) < int(0.1 * SAMPLE_RATE):
        return {"text": "", "tokens": []}
    processor, model = _load()
    inputs = processor(audio, sampling_rate=SAMPLE_RATE, return_tensors="pt").to(
        "cuda", dtype=torch.float16
    )
    with torch.no_grad():
        output = model.generate(**inputs)
    text, timestamps = processor.decode(
        output.sequences, durations=output.durations, skip_special_tokens=True
    )
    tokens = [
        {"token": item["token"], "start": float(item["start"]), "end": float(item["end"])}
        for item in timestamps[0]
    ]
    return {"text": text[0].strip(), "tokens": tokens}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="JSON com a lista de audios a transcrever")
    parser.add_argument("--output", required=True, help="JSON de saida")
    args = parser.parse_args()

    request = json.loads(Path(args.input).read_text(encoding="utf-8"))
    results = []
    for item in request["files"]:
        try:
            results.append({"id": item["id"], **transcribe(Path(item["path"]))})
        except Exception as exc:  # pragma: no cover - depende de GPU/modelo
            results.append({"id": item["id"], "error": f"{type(exc).__name__}: {exc}"})
    Path(args.output).write_text(
        json.dumps({"results": results}, ensure_ascii=False), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
