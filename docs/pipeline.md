# Pipeline de dublagem

## Objetivo

Traduzir fala inglesa diretamente para fala portuguesa, preservando entonacao na
medida suportada pelo modelo e sem alterar a velocidade, os cortes ou o codec do video
original. O pipeline de dublagem nao depende de transcricao nem de TTS.

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
o audio e `dub_segments.json` existentes, sem baixar ou traduzir novamente; basta retomar
a montagem do video que faltar.

### Extracao e separacao

O FFmpeg extrai `raw_44k_stereo.wav`. O HDemucs gera:

- `vocals.wav`, usado como referencia limpa de locutor e apoio de separacao;
- `instrumental.wav`, usado na mixagem final.

O modelo de separacao e descarregado da GPU antes de carregar o SeamlessM4T.

### Limpeza e silencio

O caminho padrao de entrada e `deepfilter_original`:

1. DeepFilterNet3 limpa o audio original e grava
   `cleaned_original_48k_mono.wav`;
2. o audio e convertido para mono/16 kHz;
3. Silero VAD detecta os blocos no sinal depois da limpeza;
4. dentro de cada bloco, um segundo VAD remove pausas a partir de 100 ms, mantendo
   30 ms de margem para evitar cortar consoantes;
5. blocos com menos de 150 ms de fala sao descartados.

Os campos `input_ms` e `silence_removed_ms` em `dub_segments.json` registram quanto
audio foi efetivamente enviado ao Seamless.

Variaveis:

```dotenv
MAX_CHUNK_DURATION_S=16
SPEECH_ONLY_MIN_SILENCE_MS=100
SPEECH_ONLY_PAD_MS=30
```

### SeamlessM4T v2

Modelo padrao: `facebook/seamless-m4t-v2-large`, entrada inglesa a 16 kHz e saida
portuguesa. Ate quatro blocos de tamanhos diferentes sao enviados na mesma chamada,
com padding e mascara de atencao produzidos pelo processor.

O decodificador do Seamless pode entrar em loops ao encontrar termos tecnicos ou
trechos ambiguos. Os testes reproduziram repeticoes mesmo com um bloco por chamada,
provando que o batch nao era a causa. O pipeline aplica os controles nativos do
decodificador de texto:

```dotenv
SEAMLESS_TEXT_REPETITION_PENALTY=1.2
SEAMLESS_TEXT_NO_REPEAT_NGRAM_SIZE=3
SEAMLESS_BATCH_SIZE=4
```

Esses parametros eliminaram, no trecho de regressao, os loops de frases como
"game em formato de PC/Android" e "nao entendo o que isso quer dizer". Eles evitam a
repeticao, mas nao garantem traducao perfeita de nomes tecnicos.

### Timeline e video

Cada saida volta ao `start` original do bloco. Se ultrapassar a janela ate a proxima
fala, somente a voz e acelerada com phase vocoder, limitada por `MAX_DUB_SPEEDUP`
(padrao `1.3`). O video nao e recortado ou retimado.

Na geracao final:

- o stream de video e copiado com `-c:v copy`;
- instrumental e voz dublada sao misturados;
- `INSTRUMENTAL_GAIN_DB` vale `-4` por padrao;
- `-shortest` impede que o container ultrapasse a duracao do video.

### Conversao opcional de voz

Com `preserve_original_voice=true`, o Seamless grava primeiro
`dubbed_seamless.wav`. O maior trecho de fala do vocal separado (maximo de 15 s) vira
`voice_reference.wav`. O Seed-VC converte o timbre e o backend normaliza sample rate e
numero de amostras antes de gravar `dubbed.wav`.

O Seed-VC roda em processo e ambiente Conda separados para nao conflitar com as
dependencias do Seamless/DeepFilterNet. O runner local usa SoundFile para salvar WAV,
evitando a exigencia de DLLs compartilhadas do FFmpeg pelo TorchCodec no Windows.
O backend chama o `python.exe` desse ambiente diretamente, sem `conda run`, pois a
ativacao do Conda pode falhar quando a API foi iniciada fora de um shell Conda. Se o
ambiente estiver em outro local, `SEED_VC_PYTHON` no `backend/.env` permite informar
o caminho exato do executavel.

## Arquivos gerados por job

- `raw_44k_stereo.wav`: audio extraido;
- `vocals.wav` e `instrumental.wav`: stems do HDemucs;
- `cleaned_original_48k_mono.wav`: saida do DeepFilterNet;
- `dub_segments.json`: timestamps e metricas dos blocos;
- `dubbed_seamless.wav`: intermediario quando Seed-VC esta ativo;
- `voice_reference.wav`: referencia para conversao de voz;
- `dubbed.wav`: voz final;
- `dubbed.mp4`: video final;
- `subtitles.srt` e `transcript.txt`: legenda e transcricao (idioma original), geradas sob
  demanda pelo botao "Gerar legenda".

## Inicializacao pelo aplicativo Electron

O instalador Electron distribui somente a interface e o codigo do backend. Na primeira
abertura, o bootstrap cria uma instalacao Miniforge por usuario e dois ambientes locais:
`backend` (Python 3.11, FFmpeg e pipeline STS) e `seedvc` (Python 3.10, isolado para
evitar conflito de versoes do Transformers). O backend e iniciado internamente em
`127.0.0.1:5501` e a interface so e aberta depois que a rota `/openapi.json` responde.

O diretorio de dados e fornecido por `OPENDUB_DATA_DIR`, portanto jobs e arquivos do
usuario nunca sao gravados dentro de `resources`, que fica somente leitura no aplicativo
instalado. O bootstrap baixa os pesos de cada modelo sob demanda. Driver NVIDIA permanece
um requisito externo: a aplicacao apenas detecta sua ausencia; nao tenta instalar driver
ou alterar configuracoes do Windows.

## Evidencia de regressao

No job curto usado durante o desenvolvimento:

- o original tinha 25,000000 s;
- o video dublado tinha 25,000000 s;
- o audio processado preservou 25,007938 s;
- DeepFilter + VAD + STS funcionaram pela rota HTTP real;
- a protecao antirrepeticao removeu os dois loops reproduzidos;
- Seed-VC executou na RTX 5050 Laptop com CUDA 13.0 e preservou o numero final de
  amostras.

Esses numeros sao evidencia do arquivo de regressao, nao uma garantia para todo video.
