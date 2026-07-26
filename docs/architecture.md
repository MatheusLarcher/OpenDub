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
4. Clique em **Dublar meu video**. A voz do proprio video e sempre usada como
   referencia: nao ha opcao a escolher.
5. A interface acompanha o progresso. A interface mostra as etapas Adicionar, Preparar,
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
| `POST /dub` | Executa/reaproveita o pipeline de dublagem. Aceita `modo` (`qualidade`, padrao, ou `rapido`). So uma dublagem pesada roda por vez. |
| `POST /generate-video` | Faz a mixagem e gera o MP4 sem retiming do video. |
| `POST /subtitles/generate` | Monta legenda/transcricao a partir dos textos da dublagem. |
| `GET /export/original/{job_id}` | Entrega o video original logo apos upload/download. |
| `GET /export/video/{job_id}` | Entrega o video dublado quando pronto. |
| `GET /export/subtitles/{job_id}` | Entrega a legenda `.srt` quando pronta. |
| `GET /export/transcript-txt/{job_id}` | Entrega a transcricao em `.txt` puro quando pronta. |

As rotas de export nomeiam o arquivo com o nome real do video (`display_name`), nao com
nomes genericos: `<nome>.mp4`, `<nome>_dublado.mp4` e `<nome>.srt`. O mesmo nome base na
legenda faz o player associa-la ao video sozinho. Em upload, o nome escolhido pelo
usuario e guardado em `original_name`, porque o arquivo e gravado como `upload.<ext>`.

Video original e legenda nao dependem do video dublado: o original vale desde a criacao
do job e a legenda desde o fim da dublagem, mesmo sem a montagem final.

Cada job fica em `backend/data/jobs/<job_id>` no desenvolvimento. No `.exe`,
`OPENDUB_DATA_DIR` redireciona esse diretorio para o perfil do usuario, pois
`resources` do aplicativo instalado e somente leitura.

O `job.json` contem pelo menos `media_path`, `source_type` e `original_name`. Os arquivos de job mais
importantes sao:

- `raw_44k_stereo.wav`: audio extraido do video;
- `vocals.wav` e `instrumental.wav`: separacao do HDemucs;
- `cleaned_original_48k_mono.wav`: original limpo pelo DeepFilterNet;
- `dub_segments.json`: blocos, timestamps e metricas de silencio;
- `fala_original/`: blocos de fala enviados ao reconhecimento;
- `voice_reference.wav`: trecho usado para clonar o timbre no TTS;
- `tts_blocks/`: tomadas de voz geradas por bloco;
- `workers/`: pedidos, respostas e logs dos ambientes de ASR e TTS;
- `dubbed.wav` e `dubbed.mp4`: resultados finais;
- `subtitles.srt` e `transcript.txt`: legenda e transcricao em portugues.

## Pipeline de audio e video

1. **Entrada.** `yt-dlp` baixa YouTube com `noplaylist=true`; upload local e copiado
   para o diretorio do job.
2. **Extracao.** FFmpeg gera audio estereo de trabalho.
3. **Separacao.** `torchaudio.pipelines.HDEMUCS_HIGH_MUSDB_PLUS` gera voz e
   instrumental. O modelo e removido da GPU antes da etapa seguinte.
4. **Reducao de ruido.** DeepFilterNet3 limpa o mix original; o sinal limpo, e nao o
   trecho com silencio bruto, e a entrada da deteccao de voz. O audio e processado em
   blocos de 30 s com cruzamento nas bordas: por ser uma rede recorrente, o cuDNN recusa
   a sequencia inteira em video longo. O modelo e descarregado da GPU em seguida, porque
   o reconhecimento e a geracao de voz rodam em outros processos e disputam a placa.
5. **VAD e recorte.** Silero VAD encontra falas; outro passe remove pausas internas
   >=100 ms, preservando 30 ms de margem. Trechos de fala menores que 150 ms sao
   ignorados.
6. **Reconhecimento.** Parakeet TDT 0.6B v3 transcreve cada bloco com tempo por token.
7. **Traducao.** Qwen3 4B (4 bits) traduz para pt-BR dentro do orcamento de caracteres
   derivado da janela de tempo do bloco.
8. **Voz.** No modo padrao, Chatterbox Multilingual V3 (pack pt-BR) gera a fala clonando
   a voz do video; cada tomada e transcrita de volta pelo Parakeet e comparada com o texto
   pedido, e abaixo de 80% de fidelidade o bloco e refeito com outra seed. No modo rapido,
   o SeamlessM4T v2 traduz fala em fala numa passagem so, com voz sintetica fixa em
   16 kHz. Os passos 6 e 7 rodam nos dois modos, entao a legenda sai igual.
9. **Timeline.** Cada fala dublada retorna ao `start` original. Se exceder a janela
   antes da proxima fala, so a voz e acelerada, no maximo 1,3x.
10. **Mux.** FFmpeg mistura instrumental e voz, copia o stream de video com `-c:v copy`
   e usa `-shortest`. O video nao e recortado ou acelerado.

## Bibliotecas e componentes

### Backend Python

| Biblioteca/componente | Uso |
| --- | --- |
| FastAPI, Uvicorn, Pydantic, `python-multipart` | API HTTP e upload. |
| `yt-dlp` | Download do video atual do YouTube. |
| FFmpeg/FFprobe | Extracao, mixagem, mux e duracoes. |
| PyTorch 2.9.1 CUDA 13.0, Torchaudio e TorchCodec | Execucao de modelos/GPU e HDemucs. |
| Transformers 4.57.1 e Safetensors | Traducao com Qwen3 4B e, no modo rapido, SeamlessM4T v2. |
| bitsandbytes | Carrega o Qwen3 4B em 4 bits, para caber em 8 GB de VRAM. |
| DeepFilterNet 0.5.6 | Reducao de ruido. |
| Silero VAD | Deteccao de fala e remocao de pausas. |
| Librosa, SoundFile, NumPy | Leitura, escrita, resample e ajuste de audio. |
| Parakeet TDT 0.6B v3 | Reconhecimento de fala e conferencia das tomadas de voz. |
| Chatterbox Multilingual V3 (pt-BR) | Geracao da voz dublada com clonagem (modo padrao). |
| SeamlessM4T v2 large | Traducao fala->fala do modo rapido. Baixado so quando esse modo e usado. |

O backend principal usa Python 3.11. O reconhecimento de fala e a geracao de voz exigem
versoes incompativeis do Transformers (`>=5.10` e `==5.2.0`), entao cada um roda em um
venv criado sobre o ambiente principal com `--system-site-packages`: os tres compartilham
o mesmo torch/CUDA e cada satelite custa algumas centenas de MB. A comunicacao com esses
ambientes e por arquivo JSON, em `backend/scripts/asr_worker.py` e `tts_worker.py`.

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
SPEECH_ONLY_MIN_SILENCE_MS=100
SPEECH_ONLY_PAD_MS=30
MAX_DUB_SPEEDUP=1.3
INSTRUMENTAL_GAIN_DB=-4

# Traducao
OPENDUB_LLM_MODEL=Qwen/Qwen3-4B-Instruct-2507
OPENDUB_CHARS_PER_SECOND=14

# Geracao de voz
OPENDUB_TTS_MIN_FIDELIDADE=0.80
OPENDUB_TTS_MAX_TENTATIVAS=3

# Ambientes de ASR e TTS (preenchidos pelo aplicativo)
OPENDUB_ASR_PYTHON=...
untimesr\Scripts\python.exe
OPENDUB_TTS_PYTHON=...
untime	ts\Scripts\python.exe
```

O backend chama o `python.exe` de cada ambiente diretamente, em vez de `conda run`: no
Windows a ativacao do Conda falha quando a API nao foi iniciada por um shell Conda.

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
cria o ambiente `backend` e os satelites `asr` e `tts`, instala FFmpeg/Torch/dependencias
e inicia a API. Modelos e checkpoints sao baixados na primeira utilizacao e reutilizados
em cache. Os marcadores de "ja instalei" guardam o hash do que foi instalado, entao um
update que mude dependencias dispara a reinstalacao sozinho.

O bootstrap nao instala driver NVIDIA: o driver precisa ser instalado pelo usuario com
o instalador oficial e permissao administrativa. Sem `nvidia-smi`, o aplicativo abre e
avisa na hora que a dublagem nao vai funcionar — os modelos de fala dependem de CUDA.

## Validacao recomendada

1. `npm run build` em `frontend`.
2. `python -m py_compile backend/main.py backend/services/jobs.py` no ambiente AI.
3. `npm run dist` para testar o empacotamento Electron/NSIS.
4. Em API real, testar upload, YouTube com URL contendo `list`, download original antes
   da dublagem, dublagem, mux e legenda.
5. Na pagina, testar F5 durante e depois de dublar; o job deve ser recuperado.
