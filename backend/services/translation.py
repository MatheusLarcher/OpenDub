"""Traduz ingles -> portugues do Brasil com o Qwen3 4B, respeitando a janela de tempo.

O ponto da traducao aqui nao e so estar correta: ela precisa CABER no tempo em que a
pessoa falou no video. Uma frase longa demais obriga a acelerar a voz (fica robotica)
ou invade a fala seguinte. Por isso passamos ao modelo o orcamento de caracteres do
bloco e pedimos que ele reescreva mais curto quando necessario.

Tambem exigimos acentuacao correta: o pack pt-BR do Chatterbox e baseado em grafemas
e, sem acento, emudece ou corta a frase (testado).
"""

from __future__ import annotations

import os
import re
from typing import List, Optional

import torch

MODEL_ID = os.getenv("OPENDUB_LLM_MODEL", "Qwen/Qwen3-4B-Instruct-2507")
# Ritmo medio da fala em pt-BR. Serve para transformar segundos em um orcamento de
# caracteres que o modelo consegue respeitar.
CHARS_PER_SECOND = float(os.getenv("OPENDUB_CHARS_PER_SECOND", "14"))
# Uma sobra pequena evita pedir frases artificialmente curtas quando o bloco ja e justo.
BUDGET_SLACK = 1.15
MIN_BUDGET_CHARS = 12

_model = None
_tokenizer = None

SYSTEM_PROMPT = (
    "Voce e um tradutor de dublagem. Traduz falas do ingles para o portugues do Brasil "
    "para serem faladas em voz alta por cima do video original.\n"
    "Regras obrigatorias:\n"
    "1. Responda APENAS com a traducao, sem aspas, sem explicacao, sem o texto em ingles.\n"
    "2. Use acentuacao e pontuacao corretas do portugues (nao, voce, video, ha, otimo).\n"
    "3. Escreva numeros por extenso (10 -> dez).\n"
    "4. Portugues brasileiro falado e natural, como a pessoa diria em voz alta.\n"
    "5. A fala precisa caber no limite de caracteres informado. Se nao couber, diga a "
    "mesma coisa de forma mais curta, mantendo o sentido.\n"
    "6. Se a fala em ingles for so uma interjeicao, traduza como interjeicao equivalente.\n"
    "7. Preserve o sentido completo, principalmente negacoes e verbos modais: "
    "\"you shouldn't be\" e \"voce nao deveria estar\", nao \"voce nao esta\"."
)


def _device() -> str:
    return "cuda" if torch.cuda.is_available() else "cpu"


def get_model():
    global _model, _tokenizer
    if _model is None:
        from transformers import AutoModelForCausalLM, AutoTokenizer

        _tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
        load_kwargs = {"dtype": torch.bfloat16}
        if _device() == "cuda":
            # Em 8 GB o modelo em bf16 (8 GB) nao deixa espaco para o cache de atencao.
            # Em 4 bits sobra folga e a qualidade da traducao se mantem.
            from transformers import BitsAndBytesConfig

            load_kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
            )
            load_kwargs["device_map"] = "cuda:0"
        _model = AutoModelForCausalLM.from_pretrained(MODEL_ID, **load_kwargs)
        _model.eval()
    return _tokenizer, _model


def unload_model() -> None:
    global _model, _tokenizer
    if _model is not None:
        del _model
        _model = None
        _tokenizer = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def budget_chars(window_s: float) -> int:
    return max(MIN_BUDGET_CHARS, int(window_s * CHARS_PER_SECOND * BUDGET_SLACK))


_UNIDADES = [
    "zero", "um", "dois", "tres", "quatro", "cinco", "seis", "sete", "oito", "nove",
    "dez", "onze", "doze", "treze", "quatorze", "quinze", "dezesseis", "dezessete",
    "dezoito", "dezenove",
]
_DEZENAS = [
    "", "", "vinte", "trinta", "quarenta", "cinquenta", "sessenta", "setenta",
    "oitenta", "noventa",
]
_CENTENAS = [
    "", "cento", "duzentos", "trezentos", "quatrocentos", "quinhentos", "seiscentos",
    "setecentos", "oitocentos", "novecentos",
]


def _por_extenso(numero: int) -> str:
    if numero < 20:
        return _UNIDADES[numero]
    if numero < 100:
        dezena, unidade = divmod(numero, 10)
        return _DEZENAS[dezena] + (f" e {_UNIDADES[unidade]}" if unidade else "")
    if numero == 100:
        return "cem"
    if numero < 1000:
        centena, resto = divmod(numero, 100)
        return _CENTENAS[centena] + (f" e {_por_extenso(resto)}" if resto else "")
    if numero < 1_000_000:
        milhar, resto = divmod(numero, 1000)
        prefixo = "mil" if milhar == 1 else f"{_por_extenso(milhar)} mil"
        return prefixo + (f" e {_por_extenso(resto)}" if resto else "")
    return str(numero)


def _expandir_numeros(text: str) -> str:
    """O modelo costuma ignorar a regra de escrever numero por extenso.

    Deixar "10" no texto faz o TTS ler o digito de forma imprevisivel, entao a
    conversao e feita aqui, de forma deterministica.
    """
    return re.sub(
        r"\b\d{1,6}\b",
        lambda m: _por_extenso(int(m.group())),
        text,
    )


def _clean(text: str) -> str:
    text = text.strip()
    # O modelo as vezes devolve a frase entre aspas ou com um rotulo na frente.
    text = re.sub(r'^(traducao|portugues|pt-br)\s*:\s*', "", text, flags=re.IGNORECASE)
    if len(text) >= 2 and text[0] in "\"'“”" and text[-1] in "\"'“”":
        text = text[1:-1].strip()
    return " ".join(_expandir_numeros(text).split())


def _generate(messages: List[dict], max_new_tokens: int) -> str:
    tokenizer, model = get_model()
    prompt = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    inputs = tokenizer([prompt], return_tensors="pt").to(model.device)
    with torch.no_grad():
        output = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            temperature=None,
            top_p=None,
            top_k=None,
            pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
        )
    generated = output[0][inputs["input_ids"].shape[-1] :]
    return _clean(tokenizer.decode(generated, skip_special_tokens=True))


def _remover_eco(traducao: str, previous_pt: Optional[str]) -> str:
    """Tira a fala anterior quando o modelo a repete antes da traducao nova.

    Dar a frase anterior como contexto melhora a continuidade, mas as vezes o modelo
    devolve "Ei. Ja uso headsets..." em vez de so a frase nova.
    """
    if not previous_pt or not traducao:
        return traducao
    anterior = previous_pt.strip()
    if not anterior or not traducao.startswith(anterior) or len(traducao) <= len(anterior):
        return traducao
    # So e eco se a frase anterior aparece inteira e FECHADA (termina em pontuacao) antes
    # da nova. Sem isso, "Nao" seguido de "Nao faca isso." viraria "faca isso." -- a
    # negacao sumiria porque a frase anterior era prefixo legitimo da nova.
    if anterior[-1] not in ".!?…":
        return traducao
    resto = traducao[len(anterior) :].lstrip()
    return resto or traducao


def translate_block(text: str, window_s: float, previous_pt: Optional[str] = None) -> str:
    """Traduz um bloco tentando caber em ``window_s`` segundos de fala."""
    text = (text or "").strip()
    if not text:
        return ""
    limit = budget_chars(window_s)
    contexto = (
        f"A fala anterior ja traduzida foi: \"{previous_pt}\"\n" if previous_pt else ""
    )
    pedido = (
        f"{contexto}Traduza para portugues do Brasil, no maximo {limit} caracteres:\n{text}"
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": pedido},
    ]
    traducao = _generate(messages, max_new_tokens=max(32, int(limit * 1.2)))
    traducao = _remover_eco(traducao, previous_pt)

    # Segunda tentativa so quando estourou de verdade; encurtar sempre piora o texto.
    if len(traducao) > limit * 1.25:
        messages.append({"role": "assistant", "content": traducao})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Ficou com {len(traducao)} caracteres e o limite e {limit}. "
                    "Reescreva mais curto, mantendo o sentido. Responda so a frase."
                ),
            }
        )
        mais_curta = _generate(messages, max_new_tokens=max(32, int(limit * 1.2)))
        if mais_curta and len(mais_curta) < len(traducao):
            traducao = mais_curta
    return traducao


def translate_blocks(blocks: List[dict]) -> List[str]:
    """blocks: [{"text": str, "window_s": float}] -> traducoes na mesma ordem."""
    traducoes: List[str] = []
    anterior: Optional[str] = None
    for index, block in enumerate(blocks):
        traducao = translate_block(block["text"], block["window_s"], anterior)
        traducoes.append(traducao)
        if traducao:
            anterior = traducao
        print(
            f"[dub] bloco {index + 1}/{len(blocks)} traduzido "
            f"({len(traducao)} car., orcamento {budget_chars(block['window_s'])})"
        )
    return traducoes
