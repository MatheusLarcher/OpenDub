from __future__ import annotations

import os
from typing import Dict, List

import torch

from backend.services import jobs, media, s2st, separation, vad

# Ate quanto a voz dublada pode acelerar (preservando tom) pra caber antes da proxima
# fala comecar, sem NUNCA sobrepor ou cortar. Video nunca e retimado -- cortes/musica
# ficam exatamente onde estavam no original; so a voz se ajusta.
MAX_DUB_SPEEDUP = float(os.getenv("MAX_DUB_SPEEDUP", "1.3"))
# Cada lote e enviado em uma unica chamada ao SeamlessM4T. Quatro blocos de ate 16 s
# mantem uma boa margem de VRAM e elimina boa parte do overhead por chamada.
SEAMLESS_BATCH_SIZE = max(1, int(os.getenv("SEAMLESS_BATCH_SIZE", "4")))


def run_dub(
    job_id: str,
    model_input: str = "deepfilter_original",
    preserve_original_voice: bool = False,
) -> List[Dict]:
    """Roda o pipeline completo: extrai audio, separa vocal/instrumental, detecta blocos
    de fala por VAD e traduz cada bloco com o SeamlessM4T v2 (eng->por). Monta
    dubbed.wav posicionando cada bloco no MESMO timestamp original (vídeo nunca e
    retimado -- so a voz e ajustada, acelerando levemente quando precisa caber antes da
    proxima fala comecar).
    """
    vocals_path, instrumental_path = separation.separate(job_id)
    # A separacao e pesada em VRAM; libera antes de carregar o SeamlessM4T v2.
    separation.unload_model()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    vocals_wav_16k = vad.load_16k_mono(vocals_path)
    if model_input == "original":
        model_wav_16k = vad.load_16k_mono(jobs.raw_audio_path(job_id))
        chunks = vad.get_chunks(vocals_wav_16k)
    elif model_input == "deepfilter_original":
        from backend.services import denoise

        model_wav_16k = vad.load_16k_mono(denoise.clean_original(job_id))
        # A deteccao e feita DEPOIS da limpeza, pois e exatamente este sinal que sera
        # enviado ao Seamless. Assim ruido removido nao vira uma falsa regiao de fala.
        chunks = vad.get_chunks(model_wav_16k)
    elif model_input == "vocals":
        model_wav_16k = vocals_wav_16k
        chunks = vad.get_chunks(vocals_wav_16k)
    else:
        raise ValueError(f"model_input invalido: {model_input}")
    total_duration = model_wav_16k.shape[-1] / vad.VAD_SAMPLE_RATE

    output_sample_rate = s2st.output_sample_rate()
    total_samples = int(total_duration * output_sample_rate)
    buffer = torch.zeros(total_samples, dtype=torch.float32)
    dub_segments: List[Dict] = []

    prepared_chunks = []
    for chunk in chunks:
        audio = vad.slice_audio(model_wav_16k, chunk["start"], chunk["end"])
        original_samples = audio.numel()
        if model_input == "deepfilter_original":
            audio = vad.remove_silence(audio)
        if audio.numel() < int(0.15 * vad.VAD_SAMPLE_RATE):
            print(f"[dub] trecho {chunk['start']:.1f}s-{chunk['end']:.1f}s ignorado: sem fala")
            continue
        prepared_chunks.append(
            {
                "chunk": chunk,
                "audio": audio.numpy(),
                "input_ms": audio.numel() / vad.VAD_SAMPLE_RATE * 1000.0,
                "silence_removed_ms": (original_samples - audio.numel()) / vad.VAD_SAMPLE_RATE * 1000.0,
            }
        )

    chunks = [item["chunk"] for item in prepared_chunks]
    for batch_start in range(0, len(prepared_chunks), SEAMLESS_BATCH_SIZE):
        batch_items = prepared_chunks[batch_start : batch_start + SEAMLESS_BATCH_SIZE]
        batch_chunks = [item["chunk"] for item in batch_items]
        batch_audio = [item["audio"] for item in batch_items]
        translated_batch, translated_sr = s2st.translate_chunks(
            batch_audio, sample_rate=vad.VAD_SAMPLE_RATE
        )
        if translated_sr != output_sample_rate:
            raise RuntimeError(
                f"Sample rate inesperado do SeamlessM4T: {translated_sr} (esperado {output_sample_rate})"
            )

        print(
            f"[dub] lote {batch_start // SEAMLESS_BATCH_SIZE + 1}/"
            f"{(len(chunks) + SEAMLESS_BATCH_SIZE - 1) // SEAMLESS_BATCH_SIZE} "
            f"({len(batch_chunks)} blocos) traduzido"
        )

        for batch_index, (item, translated) in enumerate(zip(batch_items, translated_batch)):
            index = batch_start + batch_index
            chunk = item["chunk"]
            start, end = chunk["start"], chunk["end"]
            next_start = chunks[index + 1]["start"] if index + 1 < len(chunks) else total_duration
            available_window_s = max(0.0, next_start - start)

            translated_duration_s = len(translated) / output_sample_rate
            speed_applied = 1.0
            overlapped = False
            if available_window_s > 0 and translated_duration_s > available_window_s:
                needed_speed = translated_duration_s / available_window_s
                speed_applied = min(needed_speed, MAX_DUB_SPEEDUP)
                translated = media.time_stretch(translated, speed_applied)
                translated_duration_s = len(translated) / output_sample_rate
                if translated_duration_s > available_window_s:
                    overlapped = True
                    print(
                        f"[dub] AVISO: bloco {index + 1}/{len(chunks)} ainda nao coube mesmo "
                        f"acelerado {speed_applied:.2f}x -- vai sobrepor levemente a proxima fala"
                    )

            print(
                f"[dub] bloco {index + 1}/{len(chunks)} ({start:.1f}s-{end:.1f}s) traduzido"
                + (f", acelerado {speed_applied:.2f}x" if speed_applied > 1.001 else "")
            )

            start_sample = int(start * output_sample_rate)
            translated_tensor = torch.from_numpy(translated)
            end_sample = min(total_samples, start_sample + len(translated_tensor))
            usable_len = end_sample - start_sample
            if usable_len > 0:
                buffer[start_sample:end_sample] += translated_tensor[:usable_len]

            dub_segments.append(
                {
                    "index": index,
                    "start": start,
                    "end": end,
                    "translated_ms": translated_duration_s * 1000.0,
                    "available_window_ms": available_window_s * 1000.0,
                    "speed_applied": speed_applied,
                    "overlapped": overlapped,
                    "input_ms": item["input_ms"],
                    "silence_removed_ms": item["silence_removed_ms"],
                }
            )

    seamless_output = (
        jobs.seamless_audio_path(job_id) if preserve_original_voice else jobs.dubbed_audio_path(job_id)
    )
    media.write_wav(seamless_output, buffer[None, :], output_sample_rate)
    jobs.save_json(jobs.dub_segments_path(job_id), dub_segments)

    s2st.unload_model()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    if preserve_original_voice:
        from backend.services import voice_conversion

        print("[dub] convertendo o timbre para a voz original com Seed-VC")
        voice_conversion.convert_to_original_voice(
            job_id,
            source=seamless_output,
            chunks=chunks,
            output_sample_rate=output_sample_rate,
        )

    return dub_segments
