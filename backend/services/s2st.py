from __future__ import annotations

import os
from typing import List, Optional, Tuple

import numpy as np
import torch
from transformers import AutoProcessor, SeamlessM4Tv2ForSpeechToSpeech

from backend.services._retry import with_retry

SEAMLESS_MODEL_ID = os.getenv("SEAMLESS_MODEL_ID", "facebook/seamless-m4t-v2-large")
SEAMLESS_SOURCE_LANG = os.getenv("SEAMLESS_SOURCE_LANG", "eng")
SEAMLESS_TARGET_LANG = os.getenv("SEAMLESS_TARGET_LANG", "por")
SEAMLESS_SAMPLE_RATE = 16000
SEAMLESS_TEXT_REPETITION_PENALTY = float(
    os.getenv("SEAMLESS_TEXT_REPETITION_PENALTY", "1.2")
)
SEAMLESS_TEXT_NO_REPEAT_NGRAM_SIZE = int(
    os.getenv("SEAMLESS_TEXT_NO_REPEAT_NGRAM_SIZE", "3")
)

_processor: Optional[AutoProcessor] = None
_model: Optional[SeamlessM4Tv2ForSpeechToSpeech] = None


def _device() -> torch.device:
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def get_processor() -> AutoProcessor:
    global _processor
    if _processor is None:
        _processor = AutoProcessor.from_pretrained(SEAMLESS_MODEL_ID)
    return _processor


def get_model() -> SeamlessM4Tv2ForSpeechToSpeech:
    global _model
    if _model is None:
        device = _device()
        dtype = torch.float16 if device.type == "cuda" else torch.float32

        def _load():
            # No Windows, carregar primeiro os shards de quase 5 GB em CPU e so
            # depois chamar .to(cuda) pode derrubar o processo se o pagefile estiver
            # no limite. O device_map transmite cada tensor para a GPU ao carrega-lo,
            # reduzindo bastante o pico de RAM/commit do sistema.
            load_kwargs = {"dtype": dtype}
            if device.type == "cuda":
                load_kwargs["device_map"] = "cuda:0"
            model = SeamlessM4Tv2ForSpeechToSpeech.from_pretrained(
                SEAMLESS_MODEL_ID, **load_kwargs
            )
            return model if device.type == "cuda" else model.to(device)

        _model = with_retry(_load)
        _model.eval()
    return _model


def supported_speech_target_langs() -> list[str]:
    model = get_model()
    return sorted(model.generation_config.vocoder_lang_code_to_id.keys())


def output_sample_rate() -> int:
    """Sample rate real do audio gerado pelo modelo (confirmado em teste: 16000 Hz).

    Nao usar SEAMLESS_SAMPLE_RATE (constante de ENTRADA) pra isso -- consultar o
    modelo evita depender de coincidencia caso o checkpoint mude.
    """
    return int(get_model().config.sampling_rate)


def unload_model() -> None:
    global _model
    if _model is not None:
        del _model
        _model = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def translate_chunk(
    audio: np.ndarray,
    sample_rate: int = SEAMLESS_SAMPLE_RATE,
    tgt_lang: str = SEAMLESS_TARGET_LANG,
    speaker_id: int = 0,
) -> Tuple[np.ndarray, int]:
    def _run():
        processor = get_processor()
        model = get_model()
        inputs = processor(audio=audio, sampling_rate=sample_rate, return_tensors="pt").to(model.device)
        with torch.no_grad():
            waveform, waveform_lengths = model.generate(
                **inputs,
                tgt_lang=tgt_lang,
                speaker_id=speaker_id,
                text_repetition_penalty=SEAMLESS_TEXT_REPETITION_PENALTY,
                text_no_repeat_ngram_size=SEAMLESS_TEXT_NO_REPEAT_NGRAM_SIZE,
            )
        length = int(waveform_lengths.reshape(-1)[0].item())
        output = waveform[0, :length].float().cpu().numpy()
        output_sample_rate = int(model.config.sampling_rate)
        return output, output_sample_rate

    return with_retry(_run)


def translate_chunks(
    audios: List[np.ndarray],
    sample_rate: int = SEAMLESS_SAMPLE_RATE,
    tgt_lang: str = SEAMLESS_TARGET_LANG,
    speaker_id: int = 0,
) -> Tuple[List[np.ndarray], int]:
    """Traduz varios blocos em uma unica geracao do modelo.

    O processor faz padding apenas dentro deste lote e ``waveform_lengths`` remove o
    padding de cada resultado. Assim, os blocos continuam independentes e na mesma
    ordem, mas a GPU consegue processa-los em paralelo.
    """
    if not audios:
        return [], output_sample_rate()

    def _run():
        processor = get_processor()
        model = get_model()
        inputs = processor(
            audio=audios,
            sampling_rate=sample_rate,
            return_tensors="pt",
            padding=True,
        ).to(model.device)
        with torch.no_grad():
            waveforms, waveform_lengths = model.generate(
                **inputs,
                tgt_lang=tgt_lang,
                speaker_id=speaker_id,
                text_repetition_penalty=SEAMLESS_TEXT_REPETITION_PENALTY,
                text_no_repeat_ngram_size=SEAMLESS_TEXT_NO_REPEAT_NGRAM_SIZE,
            )

        lengths = waveform_lengths.reshape(-1).tolist()
        if len(lengths) != len(audios) or waveforms.shape[0] != len(audios):
            raise RuntimeError(
                "O SeamlessM4T retornou uma quantidade de resultados diferente do lote enviado"
            )
        outputs = [
            waveforms[index, : int(length)].float().cpu().numpy()
            for index, length in enumerate(lengths)
        ]
        return outputs, int(model.config.sampling_rate)

    return with_retry(_run)
