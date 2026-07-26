from __future__ import annotations

import os
from typing import Dict, List

import torch

from backend.services import asr, jobs, media, separation, translation, tts, vad

# Ate quanto a voz dublada pode acelerar (preservando tom) pra caber antes da proxima
# fala comecar, sem NUNCA sobrepor ou cortar. Video nunca e retimado -- cortes/musica
# ficam exatamente onde estavam no original; so a voz se ajusta.
MAX_DUB_SPEEDUP = float(os.getenv("MAX_DUB_SPEEDUP", "1.3"))
# Trecho do proprio video usado como referencia para clonar a voz. O Chatterbox
# condiciona bem com ~10 s; alem disso nao melhora e so custa memoria.
REFERENCE_MAX_SECONDS = 12.0
REFERENCE_MIN_SECONDS = 3.0
MIN_SPEECH_SAMPLES_RATIO = 0.15
# Sobra tolerada antes de considerar que a fala invadiu a proxima.
OVERLAP_TOLERANCE_S = 0.02
# Quantos blocos o Seamless traduz por chamada no modo rapido.
SEAMLESS_BATCH_SIZE = max(1, int(os.getenv("SEAMLESS_BATCH_SIZE", "4")))

MODO_QUALIDADE = "qualidade"
MODO_RAPIDO = "rapido"


def _write_reference(job_id: str, clean_wav, chunks: List[vad.Chunk]):
    """Recorta o trecho de fala mais longo como referencia de timbre.

    Usamos o audio limpo pelo DeepFilterNet, nao a voz separada pelo Demucs: em teste,
    a separacao deixa artefatos que o clonador reproduz junto.
    """
    candidates = [c for c in chunks if c["end"] - c["start"] >= REFERENCE_MIN_SECONDS]
    if not candidates:
        candidates = list(chunks)
    if not candidates:
        raise RuntimeError("nenhum trecho de fala encontrado para clonar a voz")
    best = max(candidates, key=lambda c: c["end"] - c["start"])
    end = min(best["end"], best["start"] + REFERENCE_MAX_SECONDS)
    reference = vad.slice_audio(clean_wav, best["start"], end)
    path = jobs.voice_reference_path(job_id)
    media.write_wav(path, reference[None, :], vad.VAD_SAMPLE_RATE)
    return path, best


def _dub_rapido(job_id: str, prepared: List[Dict]) -> List[Dict]:
    """Modo rapido: SeamlessM4T v2 traduz fala em fala, num modelo so.

    Troca a etapa mais cara (gerar a voz clonada e conferir cada tomada) por uma unica
    passagem do Seamless. Em compensacao a voz e sintetica e fixa, em 16 kHz, e nao a
    voz da pessoa. O reconhecimento e a traducao continuam rodando, para a legenda sair
    igual nos dois modos.

    Devolve o mesmo formato de ``tts.synthesize_blocks``, entao a montagem da timeline
    nao muda.
    """
    from backend.services import s2st

    blocos_dir = jobs.job_dir(job_id) / "tts_blocks"
    blocos_dir.mkdir(exist_ok=True)
    resultados: List[Dict] = []
    total_lotes = (len(prepared) + SEAMLESS_BATCH_SIZE - 1) // SEAMLESS_BATCH_SIZE
    for inicio in range(0, len(prepared), SEAMLESS_BATCH_SIZE):
        lote = prepared[inicio : inicio + SEAMLESS_BATCH_SIZE]
        audios = [vad.load_16k_mono(item["path"]).numpy() for item in lote]
        traduzidos, taxa = s2st.translate_chunks(audios, sample_rate=vad.VAD_SAMPLE_RATE)
        for deslocamento, audio in enumerate(traduzidos):
            indice = inicio + deslocamento
            destino = blocos_dir / f"bloco_{indice:03d}_rapido.wav"
            media.write_wav(destino, torch.from_numpy(audio)[None, :], taxa)
            resultados.append(
                {
                    "path": str(destino),
                    "duration_s": len(audio) / taxa,
                    "sample_rate": taxa,
                    # Sem clonagem nao ha tomada para conferir: o Seamless nao colapsa
                    # como o TTS, entao nao existe o laco de repeticao aqui.
                    "fidelidade": 1.0,
                    "tentativas": 1,
                }
            )
        print(f"[dub] lote {inicio // SEAMLESS_BATCH_SIZE + 1}/{total_lotes} traduzido")
    s2st.unload_model()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return resultados


def run_dub(job_id: str, modo: str = MODO_QUALIDADE) -> List[Dict]:
    """Dubla o video em cascata: reconhece a fala, traduz e recria a voz.

    Cada bloco volta para o MESMO timestamp em que a pessoa falou no original. O video
    nunca e retimado; quando a frase em portugues fica mais longa que a janela, so a
    voz acelera um pouco (tom preservado), ate o teto de MAX_DUB_SPEEDUP.

    ``modo`` escolhe entre "qualidade" (padrao: reconhece, traduz e recria a voz da
    pessoa) e "rapido" (SeamlessM4T v2 traduz fala em fala, cerca de tres vezes mais
    rapido, com voz sintetica fixa).
    """
    if modo not in {MODO_QUALIDADE, MODO_RAPIDO}:
        raise ValueError(f"modo invalido: {modo}")
    vocals_path, _instrumental_path = separation.separate(job_id)
    # A separacao e pesada em VRAM e nada depois dela precisa do Demucs carregado.
    separation.unload_model()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    from backend.services import denoise

    clean_wav = vad.load_16k_mono(denoise.clean_original(job_id))
    # ASR e TTS rodam em outros processos e disputam a mesma placa: nada aqui pode
    # continuar segurando VRAM depois que a limpeza terminou.
    denoise.unload_model()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    # A deteccao roda DEPOIS da limpeza: ruido removido nao vira falsa regiao de fala.
    chunks = vad.get_chunks(clean_wav)
    total_duration = clean_wav.shape[-1] / vad.VAD_SAMPLE_RATE

    segments_dir = jobs.job_dir(job_id) / "fala_original"
    segments_dir.mkdir(exist_ok=True)
    prepared: List[Dict] = []
    for chunk in chunks:
        audio = vad.slice_audio(clean_wav, chunk["start"], chunk["end"])
        original_samples = audio.numel()
        audio = vad.remove_silence(audio)
        if audio.numel() < int(MIN_SPEECH_SAMPLES_RATIO * vad.VAD_SAMPLE_RATE):
            print(f"[dub] trecho {chunk['start']:.1f}s-{chunk['end']:.1f}s ignorado: sem fala")
            continue
        path = segments_dir / f"bloco_{len(prepared):03d}.wav"
        media.write_wav(path, audio[None, :], vad.VAD_SAMPLE_RATE)
        prepared.append(
            {
                "chunk": chunk,
                "path": path,
                "input_ms": audio.numel() / vad.VAD_SAMPLE_RATE * 1000.0,
                "silence_removed_ms": (original_samples - audio.numel())
                / vad.VAD_SAMPLE_RATE
                * 1000.0,
            }
        )

    if not prepared:
        raise RuntimeError("nenhuma fala encontrada no video")

    reference_path, _reference_chunk = _write_reference(job_id, clean_wav, [i["chunk"] for i in prepared])

    print(f"[dub] transcrevendo {len(prepared)} blocos de fala")
    transcricoes = asr.transcribe_segments(job_id, [item["path"] for item in prepared])

    # A janela de cada bloco vai do inicio dele ate o inicio do proximo: e o espaco
    # real disponivel para falar sem atropelar a fala seguinte.
    janelas: List[float] = []
    for index, item in enumerate(prepared):
        start = item["chunk"]["start"]
        next_start = (
            prepared[index + 1]["chunk"]["start"] if index + 1 < len(prepared) else total_duration
        )
        janelas.append(max(0.0, next_start - start))

    print("[dub] traduzindo para portugues")
    traducoes = translation.translate_blocks(
        [
            {"text": transcricoes[index]["text"], "window_s": janelas[index]}
            for index in range(len(prepared))
        ]
    )
    translation.unload_model()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    if modo == MODO_RAPIDO:
        print("[dub] modo rapido: traduzindo a fala direto com o Seamless")
        vozes = _dub_rapido(job_id, prepared)
    else:
        print("[dub] gerando a voz em portugues")
        vozes = tts.synthesize_blocks(job_id, reference_path, traducoes)

    sample_rate = next(
        (item["sample_rate"] for item in vozes if item["path"] and item["sample_rate"]), 24000
    )
    total_samples = int(total_duration * sample_rate)
    buffer = torch.zeros(total_samples, dtype=torch.float32)
    dub_segments: List[Dict] = []

    for index, item in enumerate(prepared):
        chunk = item["chunk"]
        start, end = chunk["start"], chunk["end"]
        janela = janelas[index]
        voz = vozes[index]
        speed_applied = 1.0
        overlapped = False
        duracao_s = 0.0

        if voz["path"]:
            wave, wave_sr = media.read_wav(voz["path"])
            if wave.shape[0] > 1:
                wave = wave.mean(dim=0, keepdim=True)
            audio = wave[0].numpy()
            if wave_sr != sample_rate:
                import librosa

                audio = librosa.resample(audio, orig_sr=wave_sr, target_sr=sample_rate)
            duracao_s = len(audio) / sample_rate
            if janela > 0 and duracao_s > janela:
                speed_applied = min(duracao_s / janela, MAX_DUB_SPEEDUP)
                audio = media.time_stretch(audio, speed_applied)
                duracao_s = len(audio) / sample_rate
                # O phase vocoder devolve alguns milissegundos a mais que o alvo; sem uma
                # tolerancia, todo bloco acelerado seria marcado como sobreposto.
                if duracao_s > janela + OVERLAP_TOLERANCE_S:
                    overlapped = True
                    print(
                        f"[dub] AVISO: bloco {index + 1}/{len(prepared)} nao coube mesmo "
                        f"acelerado {speed_applied:.2f}x -- vai sobrepor levemente"
                    )
            start_sample = int(start * sample_rate)
            end_sample = min(total_samples, start_sample + len(audio))
            usable = end_sample - start_sample
            if usable > 0:
                buffer[start_sample:end_sample] += torch.from_numpy(audio[:usable])

        dub_segments.append(
            {
                "index": index,
                "start": start,
                "end": end,
                "text_en": transcricoes[index]["text"],
                "text_pt": traducoes[index],
                "translated_ms": duracao_s * 1000.0,
                "available_window_ms": janela * 1000.0,
                "speed_applied": speed_applied,
                "overlapped": overlapped,
                "fidelidade": voz["fidelidade"],
                "tentativas": voz["tentativas"],
                "input_ms": item["input_ms"],
                "silence_removed_ms": item["silence_removed_ms"],
            }
        )

    media.write_wav(jobs.dubbed_audio_path(job_id), buffer[None, :], sample_rate)
    jobs.save_json(jobs.dub_segments_path(job_id), dub_segments)
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return dub_segments
