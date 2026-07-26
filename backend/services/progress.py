"""Progresso real da dublagem, para a barra da interface nao ficar mentindo.

A barra antiga avancava so com o relogio (`1 - e^(-t/45)`): em dois minutos ja estava
em 95% e ficava visualmente parada pelo resto do processo -- num video de 14 min, isso
sao mais de 40 minutos de barra imovel.

Aqui o andamento vem do trabalho de verdade. Os pesos abaixo foram medidos cronometrando
as fases de um video de 14 min (44 min de dublagem): a geracao de voz sozinha e 3/4 do
tempo, entao e dela que precisa vir o sinal fino. As demais entram como marcos.

O sinal fino sai de graca: o worker de voz grava um wav por bloco assim que termina cada
um, entao contar arquivos na pasta ja diz quantos blocos ficaram prontos -- sem mudar o
protocolo dos workers nem criar canal novo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

# (nome da fase, peso). Somam 1.0.
FASES = (
    ("separando", 0.08),
    ("limpando", 0.02),
    ("reconhecendo", 0.02),
    ("traduzindo", 0.11),
    ("gerando_voz", 0.75),
    ("montando", 0.02),
)
_BASES: Dict[str, float] = {}
_PESOS: Dict[str, float] = {}
_acumulado = 0.0
for _nome, _peso in FASES:
    _BASES[_nome] = _acumulado
    _PESOS[_nome] = _peso
    _acumulado += _peso

# Quanto da fatia de voz ainda sobra depois de cada passada. A primeira passada gera
# todos os blocos e leva a maior parte (60%); cada retentativa consome 60% do que restou.
# E o que impede a barra de ANDAR PARA TRAS: a retentativa nao recomeca a contagem, ela
# ocupa a sobra da anterior. Contar "prontos/total" a cada passada foi o que fez a versao
# antiga regredir, porque o total mudava quando um bloco reprovava na conferencia.
_SOBRA_POR_PASSADA = 0.4

_estado: Dict[str, Dict] = {}


def iniciar(job_id: str) -> None:
    _estado[job_id] = {"fase": FASES[0][0], "tentativa": 0, "total": 0, "blocks_dir": None}


def fase(job_id: str, nome: str) -> None:
    atual = _estado.get(job_id)
    if atual is None:
        iniciar(job_id)
        atual = _estado[job_id]
    atual["fase"] = nome


def tentativa_de_voz(job_id: str, tentativa: int, total: int, blocks_dir: Path) -> None:
    """Cada passada do TTS avisa quantos blocos vai gerar e onde eles caem."""
    atual = _estado.get(job_id)
    if atual is None:
        return
    atual.update({"fase": "gerando_voz", "tentativa": tentativa, "total": total, "blocks_dir": blocks_dir})


def concluir(job_id: str) -> None:
    _estado.pop(job_id, None)


def _fracao_da_voz(atual: Dict) -> float:
    """Onde estamos dentro da fatia de geracao de voz, entre 0 e 1."""
    tentativa = atual.get("tentativa", 0)
    inicio = 1.0 - _SOBRA_POR_PASSADA ** tentativa
    fim = 1.0 - _SOBRA_POR_PASSADA ** (tentativa + 1)
    total = atual.get("total") or 0
    blocks_dir = atual.get("blocks_dir")
    if total <= 0 or blocks_dir is None:
        return inicio
    try:
        prontos = len(list(Path(blocks_dir).glob(f"*_t{tentativa}.wav")))
    except OSError:
        return inicio
    return inicio + (fim - inicio) * min(1.0, prontos / total)


def ler(job_id: str) -> Optional[Dict[str, float]]:
    """Progresso 0..1 e o teto da fase atual.

    O teto existe para a interface: ela pode continuar avancando sozinha ate o fim da
    fase quando o backend fica quieto (fases sem sinal fino demoram minutos), mas nunca
    invade a fase seguinte -- que e exatamente onde a barra antiga passava a mentir.
    """
    atual = _estado.get(job_id)
    if atual is None:
        return None
    nome = atual["fase"]
    base, peso = _BASES.get(nome, 0.0), _PESOS.get(nome, 0.0)
    dentro = _fracao_da_voz(atual) if nome == "gerando_voz" else 0.0
    return {"progresso": round(base + peso * dentro, 4), "teto": round(base + peso, 4)}
