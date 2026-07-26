# Pipeline de dublagem

## Objetivo

Traduzir a fala inglesa para portugues brasileiro na propria voz da pessoa do video, sem
alterar a velocidade, os cortes ou o codec do video original.

## Como funciona

Ate julho de 2026 a dublagem era feita por um unico modelo fala->fala (SeamlessM4T v2),
que devolvia uma voz sintetica fixa em 16 kHz; o Seed-VC entrava depois, opcionalmente,
para aproximar o timbre original. Hoje o caminho e uma cascata de tres etapas:

| Etapa | Modelo | Licenca |
|---|---|---|
| Reconhecer a fala | `nvidia/parakeet-tdt-0.6b-v3` | CC-BY-4.0 |
| Traduzir | `Qwen/Qwen3-4B-Instruct-2507` (4 bits) | Apache-2.0 |
| Gerar a voz | Chatterbox Multilingual V3, pack pt-BR | MIT |

A clonagem passou a ser nativa do TTS: nao existe mais opcao de "manter entonacao
original", porque a voz do video e sempre a referencia. A saida subiu de 16 kHz para
24 kHz.

## Etapas

### Entrada do YouTube

Cada URL cria exatamente um job. O yt-dlp roda com `noplaylist=true`, portanto uma URL
de video que tambem contenha parametros como `list`, `index` ou `start_radio` baixa
somente o video indicado por `v`.

O job registra `source_type: youtube`; por isso a interface disponibiliza o download do
video original logo que o download termina, sem depender de `/dub`. Uploads locais sao
marcados como `source_type: upload`.

### Recuperacao depois de recarregar a pagina

O navegador guarda o ID do job ativo. Depois de F5 ou de reabrir a pagina, ele consulta
`GET /jobs/{job_id}/status`, restaura os blocos e arquivos ja prontos e continua exibindo
uma dublagem que ainda esta em andamento. Se a dublagem ja terminou, `POST /dub` reutiliza
o audio e `dub_segments.json` existentes.

### Extracao e separacao

O FFmpeg extrai `raw_44k_stereo.wav`. O HDemucs gera `vocals.wav` e `instrumental.wav`
(este ultimo entra na mixagem final). O modelo de separacao e descarregado da GPU antes
da etapa seguinte.

### Limpeza e deteccao de fala

1. DeepFilterNet3 limpa o audio original e grava `cleaned_original_48k_mono.wav`;
2. o audio e convertido para mono/16 kHz;
3. Silero VAD detecta os blocos no sinal ja limpo, cortando em silencios de 350 ms ou
   mais, com 80 ms de folga em cada ponta e um teto de 16 s por bloco;
4. blocos com menos de 150 ms sao descartados.

O bloco vai inteiro para o reconhecimento, com as pausas internas. Havia um segundo passe
que removia pausas a partir de 100 ms, herdado do motor fala->fala (que copiava a
estrutura de pausa da entrada para a saida). Medido no mesmo audio, ele mudava a
transcricao em 7 de 11 blocos e chegava a quebrar palavra -- "A six DOF" virou "A six
deal F" -- para economizar 7% de audio, ou 0,24 s de processamento. Foi removido.

```dotenv
MAX_CHUNK_DURATION_S=16
```

**O fatiamento nao corta no meio da palavra.** Medindo a energia do audio nos 42 pontos de
corte de um clipe de 3 min: 41 caem no silencio e 1 fica na fronteira do piso de ruido
(rms 0,048 contra limite 0,0477). Nenhum bloco bateu no teto de 16 s, que e o unico caso
em que o corte seria forcado no meio da fala.

Fatiar tambem **melhora** o reconhecimento em vez de piorar. No mesmo clipe, transcrever o
audio inteiro numa passagem contra fatiado em blocos:

| | Inteiro | Fatiado |
|---|---|---|
| Palavras | 184 | **215** |
| Pontuacoes | 6 | **31** |
| Palavras com maiuscula | 10 | **54** |

Em audio longo o modelo sai da distribuicao em que foi treinado (trechos de fala) e
degenera num fluxo sem pontuacao nem caixa alta, perdendo palavras. A pontuacao importa
duas vezes depois: a traducao usa a frase como contexto e o TTS pt-BR depende dela.

### Referencia de voz

O trecho de fala mais longo (ate 12 s) vira `voice_reference.wav` e condiciona o TTS.
Ele e recortado do audio limpo pelo DeepFilterNet, **nao** do `vocals.wav` separado pelo
Demucs: a separacao deixa artefatos que o clonador reproduz junto.

### Reconhecimento (Parakeet TDT)

Cada bloco e transcrito com tempo por token. O modelo ocupa cerca de 1,3 GB de VRAM e
transcreve 15 s de audio em torno de 2 s.

### Traducao com orcamento de tempo (Qwen3 4B)

A traducao precisa caber no tempo em que a pessoa falou: frase longa demais obriga a
acelerar a voz ou invade a fala seguinte. O modelo recebe a janela do bloco convertida em
um orcamento de caracteres (`OPENDUB_CHARS_PER_SECOND`, padrao 14) e reescreve mais curto
quando estoura.

Dois detalhes que vieram de falhas reais:

- **acentuacao e obrigatoria** — o pack pt-BR do Chatterbox e baseado em grafemas; com
  "voce" e "nao" sem acento ele emudece ou corta a frase;
- **numeros sao expandidos por extenso no codigo**, nao so pedidos no prompt, porque o
  modelo ignorava a regra e deixava "10" no texto.

```dotenv
OPENDUB_LLM_MODEL=Qwen/Qwen3-4B-Instruct-2507
OPENDUB_CHARS_PER_SECOND=14
```

O modelo e carregado em 4 bits: em bf16 ele sozinho ocupa os 8 GB de uma placa de entrada
e nao sobra espaco para o cache de atencao.

### Geracao da voz (Chatterbox pt-BR) e conferencia

Cada bloco e sintetizado com a voz do video como referencia. Textos acima de 120
caracteres sao fatiados por frase e concatenados: em texto longo o modelo perde o fio
(medido: 3% de fidelidade com 133 caracteres de uma vez).

O modelo colapsa de vez em quando — devolve quase silencio, ou uma frase corrompida com
duracao plausivel. Checar so a duracao nao pega o segundo caso: uma tomada de 6,8 s
(esperado ~10 s) falou "eu vou te contar porque eu estimular". Por isso **cada tomada e
transcrita de volta pelo Parakeet e comparada com o texto pedido**; abaixo do limite de
fidelidade, o bloco e refeito com outra seed. Tomadas boas ficam em torno de 98% e as
ruins em 51% ou menos, entao o corte padrao e 80%.

Interjeicoes ("Ei", "Ha") nao dao material para o ASR comparar e sao avaliadas so por ter
saido som.

```dotenv
OPENDUB_TTS_MIN_FIDELIDADE=0.80
OPENDUB_TTS_MAX_TENTATIVAS=3
```

`cfg_weight` fica fixo em 0.5. Reduzir acelera cerca de 23%, mas truncou a frase em todas
as tomadas medidas (51% de fidelidade).

### Timeline e video

Cada bloco volta ao `start` original. Se ultrapassar a janela ate a proxima fala, somente
a voz e acelerada com phase vocoder, limitada por `MAX_DUB_SPEEDUP` (padrao `1.3`). O
video nunca e recortado ou retimado.

Na geracao final o stream de video e copiado com `-c:v copy`, instrumental e voz sao
misturados (`INSTRUMENTAL_GAIN_DB`, padrao `-4`) e `-shortest` impede que o container
ultrapasse a duracao do video.

### Legenda

A legenda nao roda modelo nenhum: ela e montada com o que a dublagem ja reconheceu e
traduziu. Sai em portugues, na hora. Antes disso, um Whisper proprio transcrevia o audio
original de novo — mais um modelo para baixar, mais de um minuto de espera e a legenda
saia em ingles.

## Ambientes Python

O Parakeet TDT exige `transformers >= 5.10` e o Chatterbox fixa `transformers == 5.2.0`.
Nao existe versao que atenda aos dois, entao cada um roda em um venv proprio criado sobre
o ambiente principal com `--system-site-packages`: os tres compartilham o mesmo torch/CUDA
e cada satelite custa algumas centenas de MB em vez de outra stack CUDA inteira.

O backend chama o `python.exe` de cada ambiente diretamente, sem `conda run`, e conversa
com eles por arquivo JSON (`backend/scripts/asr_worker.py` e `tts_worker.py`).

```dotenv
OPENDUB_ASR_PYTHON=...\runtime\asr\Scripts\python.exe
OPENDUB_TTS_PYTHON=...\runtime\tts\Scripts\python.exe
```

## Arquivos gerados por job

- `raw_44k_stereo.wav`: audio extraido;
- `vocals.wav` e `instrumental.wav`: stems do HDemucs;
- `cleaned_original_48k_mono.wav`: saida do DeepFilterNet;
- `fala_original/bloco_NNN.wav`: blocos de fala enviados ao reconhecimento;
- `voice_reference.wav`: trecho usado para clonar o timbre;
- `tts_blocks/`: tomadas geradas por bloco;
- `workers/`: pedidos, respostas e logs dos ambientes de ASR e TTS;
- `dub_segments.json`: tempos, texto em ingles e portugues, fidelidade e tentativas;
- `dubbed.wav`: voz final (24 kHz);
- `dubbed.mp4`: video final;
- `subtitles.srt` e `transcript.txt`: legenda e transcricao em portugues.

Os downloads recebem o nome real do video (titulo do YouTube ou nome do arquivo
enviado, guardado em `original_name`): `<nome>.mp4` para o original,
`<nome>_dublado.mp4` para o dublado e `<nome>.srt` para a legenda -- o mesmo nome base
faz o player casar legenda e video sozinho.

Video original e legenda ficam disponiveis antes do video dublado: o original assim que
o job e criado, a legenda assim que a dublagem termina, mesmo que a montagem final ainda
nao tenha rodado.

## Inicializacao pelo aplicativo Electron

O instalador distribui somente a interface e o codigo do backend. Na primeira abertura o
bootstrap cria uma instalacao Miniforge por usuario, o ambiente `backend` (Python 3.11 e
FFmpeg) e os dois satelites `asr` e `tts`. O backend sobe em `127.0.0.1:5501` e a
interface so abre depois que `/openapi.json` responde.

Os marcadores `.backend-ready`, `.asr-ready` e `.tts-ready` guardam o **hash** do que foi
instalado, nao um simples "ok". Antes, uma maquina que ja tinha aberto uma versao anterior
mantinha o marcador antigo e o bootstrap pulava o `pip install` inteiro — o app subia com
as dependencias do update passado. Com o hash, mudar `requirements.txt` ou a lista de
pacotes de um satelite dispara a reinstalacao sozinha.

O bootstrap tambem apaga o ambiente do Seed-VC quando encontra sobra de versoes anteriores
(cerca de 3,7 GB com uma stack CUDA inteira que nada mais usa).

O diretorio de dados vem de `OPENDUB_DATA_DIR`, entao jobs e arquivos do usuario nunca sao
gravados dentro de `resources`, que fica somente leitura no aplicativo instalado. Driver
NVIDIA continua sendo requisito externo, e agora a ausencia dele e avisada na abertura: a
dublagem depende de CUDA.

## Medicoes de referencia

Todas em uma RTX 5050 Laptop (8 GB), com os modelos ja baixados.

Custo por etapa, clipe de 15 s no modo padrao:

| Etapa | Tempo |
|---|---|
| Extrair audio | 0,1 s |
| Separar (Demucs) | 4,4 s |
| Limpar (DeepFilterNet) | 1,8 s |
| Detectar fala (VAD) | 0,4 s |
| Reconhecer (Parakeet) | ~2 s |
| Traduzir (Qwen3 4B) | ~4 s |
| Gerar voz + conferir | ~100 s |
| Montar o video | 0,3 s |
| **Total pela interface** | **~126 s** |

Gerar a voz domina o custo: e a etapa mais lenta de todo o pipeline.

Video mais longo, 3 min de um tutorial real, modo padrao: 353 s, 42 blocos, nenhum
bloco mudo, fidelidade media de 83%. Os blocos abaixo do corte vinham de fala
bilingue (ingles misturado com tagalog), que o reconhecimento ja entrega embaralhada.

A limpeza de ruido escala bem depois do fatiamento: 22,7 min de audio em 19 s, com
pico de 0,28 GB de VRAM.

Sao numeros dos arquivos de regressao, nao garantia para todo video.
