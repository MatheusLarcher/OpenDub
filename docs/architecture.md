# Arquitetura e operacao

## Visao geral

O OpenDub e uma aplicacao local Windows para transformar video em ingles em
video dublado em portugues. A traducao principal e **fala para fala (STS)**; nao ha
STT+TTS no caminho de dublagem. O objetivo operacional e preservar o stream de video,
os timestamps das falas, a musica e os efeitos do original.

O sistema tem tres camadas:

```text
React/Vite (pagina) -> FastAPI local :5501 -> pipeline Python/GPU
       ^                         |
       |                         +-> jobs no disco, modelos e FFmpeg
Electron (.exe) -----------------+
```

Em desenvolvimento, a pagina roda em `http://localhost:5500` e a API em
`http://localhost:5501`. No aplicativo Electron, a API e iniciada pelo processo
principal antes de a janela da interface abrir.

## Fluxo para quem usa a pagina

1. Cole um link do YouTube ou arraste/escolha um arquivo de video.
2. Clique em **Continuar**. A pagina cria um job e guarda seu ID no `localStorage`.
3. Para YouTube, assim que o download termina aparece **Baixar video original**; nao
   e preciso iniciar a dublagem para ter acesso a esse arquivo.
4. Escolha, se quiser, **Manter entonacao original**. Essa opcao ativa Seed-VC e
   aumenta o tempo de processamento.
5. Clique em **Dublar meu video**. A interface mostra as etapas Adicionar, Preparar,
   Dublar e Finalizar, bloqueando botoes duplicados enquanto ha uma requisicao ativa.
6. Ao terminar, baixe video dublado, video original e legenda `.srt` (a legenda e
   solicitada separadamente).

Se a pagina for recarregada, ela consulta o status do job a cada 2 segundos. Arquivos
ja prontos continuam disponiveis e uma dublagem em andamento continua no backend.

## API e persistencia de job

| Rota | Responsabilidade |
| --- | --- |
| `POST /process/youtube` | Baixa somente o video indicado na URL, mesmo com parametros de playlist. Salva `source_type: youtube`. |
| `POST /process/upload` | Copia o arquivo local para o job e salva `source_type: upload`. |
| `GET /jobs/{job_id}/status` | Informa midia, audio dublado, video, legenda, processamento em curso e origem. |
| `POST /dub` | Executa/reaproveita o pipeline de dublagem. So uma dublagem pesada roda por vez. |
| `POST /generate-video` | Faz a mixagem e gera o MP4 sem retiming do video. |
| `POST /subtitles/generate` | Gera transcricao/legenda somente quando solicitada. |
| `GET /export/original/{job_id}` | Entrega o video original logo apos upload/download. |
| `GET /export/video/{job_id}` | Entrega `dubbed.mp4` quando pronto. |
| `GET /export/subtitles/{job_id}` | Entrega a legenda `.srt` quando pronta. |
| `GET /export/transcript-txt/{job_id}` | Entrega a transcricao em `.txt` puro quando pronta. |

Cada job fica em `backend/data/jobs/<job_id>` no desenvolvimento. No `.exe`,
`OPENDUB_DATA_DIR` redireciona esse diretorio para o perfil do usuario, pois
`resources` do aplicativo instalado e somente leitura.

O `job.json` contem pelo menos `media_path` e `source_type`. Os arquivos de job mais
importantes sao:

- `raw_44k_stereo.wav`: audio extraido do video;
- `vocals.wav` e `instrumental.wav`: separacao do HDemucs;
- `cleaned_original_48k_mono.wav`: original limpo pelo DeepFilterNet;
- `dub_segments.json`: blocos, timestamps e metricas de silencio;
- `dubbed_seamless.wav`: resultado STS antes do Seed-VC;
- `voice_reference.wav`: maior trecho vocal usado pelo Seed-VC;
- `dubbed.wav` e `dubbed.mp4`: resultados finais;
- `subtitles.srt` e `transcript.txt`: legenda e transcricao, quando solicitadas;
- `seed_vc.log`: stdout/stderr da conversao de voz, quando habilitada.

## Pipeline de audio e video

1. **Entrada.** `yt-dlp` baixa YouTube com `noplaylist=true`; upload local e copiado
   para o diretorio do job.
2. **Extracao.** FFmpeg gera audio estereo de trabalho.
3. **Separacao.** `torchaudio.pipelines.HDEMUCS_HIGH_MUSDB_PLUS` gera voz e
   instrumental. O modelo e removido da GPU antes do Seamless.
4. **Reducao de ruido.** DeepFilterNet3 limpa o mix original; o sinal limpo, e nao o
   trecho com silencio bruto, e a entrada da deteccao de voz.
5. **VAD e recorte.** Silero VAD encontra falas; outro passe remove pausas internas
   >=100 ms, preservando 30 ms de margem. Trechos de fala menores que 150 ms sao
   ignorados.
6. **Traducao STS.** SeamlessM4T v2 large recebe ingles em 16 kHz e gera portugues.
   Quatro blocos podem ser processados em lote, com padding/mascara corretos. A geracao
   usa penalidade de repeticao e bloqueio de n-gramas para reduzir alucinacoes em loop.
7. **Timeline.** Cada fala dublada retorna ao `start` original. Se exceder a janela
   antes da proxima fala, so a voz e acelerada, no maximo 1,3x.
8. **Voz opcional.** Seed-VC recebe o portugues do Seamless e a referencia vocal do
   video. O timbre e convertido sem recorrer a TTS; o numero final de amostras e
   normalizado para o mesmo tamanho da saida Seamless.
9. **Mux.** FFmpeg mistura instrumental e voz, copia o stream de video com `-c:v copy`
   e usa `-shortest`. O video nao e recortado ou acelerado.

## Bibliotecas e componentes

### Backend Python

| Biblioteca/componente | Uso |
| --- | --- |
| FastAPI, Uvicorn, Pydantic, `python-multipart` | API HTTP e upload. |
| `yt-dlp` | Download do video atual do YouTube. |
| FFmpeg/FFprobe | Extracao, mixagem, mux e duracoes. |
| PyTorch 2.9.1 CUDA 13.0, Torchaudio e TorchCodec | Execucao de modelos/GPU e HDemucs. |
| Transformers 4.57.1 e Safetensors | SeamlessM4T v2 large. |
| DeepFilterNet 0.5.6 | Reducao de ruido. |
| Silero VAD | Deteccao de fala e remocao de pausas. |
| Librosa, SoundFile, NumPy | Leitura, escrita, resample e ajuste de audio. |
| Faster-Whisper | Legenda/transcricao opcional. |
| Seed-VC | Conversao opcional para aproximar o timbre original. |

O backend principal usa Python 3.11. Seed-VC fica em Python 3.10 separado, pois usa
Transformers 4.46.3 e dependencias proprias (`accelerate`, `scipy`, `munch`, `einops`,
`descript-audio-codec`, `resemblyzer`, `modelscope`, `funasr`, `hydra-core` e outras
listadas em `backend/seedvc-requirements.txt`).

### Frontend e desktop

| Biblioteca/componente | Uso |
| --- | --- |
| React 18 | Estado e interface. |
| Vite | Servidor de desenvolvimento e build estatico. |
| Framer Motion | Progresso, entradas/saidas de cards, resultado e spinners. |
| Electron 30 | Janela desktop e inicio do backend local. |
| Electron Builder/NSIS | Empacotamento do instalador Windows. |

## Configuracao e variaveis importantes

```dotenv
# Dublagem
MAX_CHUNK_DURATION_S=16
SEAMLESS_BATCH_SIZE=4
SEAMLESS_TEXT_REPETITION_PENALTY=1.2
SEAMLESS_TEXT_NO_REPEAT_NGRAM_SIZE=3
SPEECH_ONLY_MIN_SILENCE_MS=100
SPEECH_ONLY_PAD_MS=30
MAX_DUB_SPEEDUP=1.3
INSTRUMENTAL_GAIN_DB=-4

# Seed-VC opcional
SEED_VC_DIR=C:\caminho\seed-vc
SEED_VC_PYTHON=C:\caminho\envs\seedvc\python.exe
SEED_VC_STEPS=30
```

`SEED_VC_PYTHON` e preferivel a `conda run` no Windows: o backend chama o
`python.exe` diretamente e evita falhas de `conda activate` em processo filho.

## Executar, construir e distribuir

### Desenvolvimento

```powershell
conda activate AI
pip install --index-url https://download.pytorch.org/whl/cu130 torch==2.9.1+cu130 torchaudio==2.9.1+cu130 torchcodec==0.15.0+cu130
pip install -r backend/requirements.txt
cd frontend
npm install
cd ..
.\start.bat
```

### Instalador leve

```powershell
.\build_frontend_exe.bat
```

O NSIS e criado em `frontend/release`. O instalador contem frontend, Electron e fontes
do backend, mas nao os modelos nem Python. Na primeira abertura ele baixa Miniforge,
cria os ambientes `backend` e `seedvc`, instala FFmpeg/Torch/dependencias e inicia a
API. Modelos e checkpoints sao baixados na primeira utilizacao e reutilizados em cache.

O bootstrap nao instala driver NVIDIA: o driver precisa ser instalado pelo usuario com
o instalador oficial e permissao administrativa. Sem `nvidia-smi`, o aplicativo abre,
mas a dublagem tende a cair para CPU e ficar muito mais lenta.

## Validacao recomendada

1. `npm run build` em `frontend`.
2. `python -m py_compile backend/main.py backend/services/jobs.py` no ambiente AI.
3. `npm run dist` para testar o empacotamento Electron/NSIS.
4. Em API real, testar upload, YouTube com URL contendo `list`, download original antes
   da dublagem, dublagem, mux e legenda.
5. Na pagina, testar F5 durante e depois de dublar; o job deve ser recuperado.
