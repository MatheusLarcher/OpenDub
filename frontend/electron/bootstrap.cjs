const { spawn } = require("child_process");
const fs = require("fs/promises");
const fssync = require("fs");
const crypto = require("crypto");
const https = require("https");
const path = require("path");

const MINIFORGE_URL = "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Windows-x86_64.exe";
const TORCH_INDEX = "https://download.pytorch.org/whl/cu130";
const TORCH_PACKAGES = ["torch==2.9.1+cu130", "torchaudio==2.9.1+cu130", "torchcodec==0.15.0+cu130"];
// Instalados pelo conda no ambiente principal. Entram na assinatura do ambiente logo
// abaixo: sem isso, mudar esta lista nao chegaria em quem ja tem o app instalado -- o
// marcador continuaria batendo e a etapa inteira seria pulada no update.
const CONDA_PACKAGES = ["ffmpeg", "deno"];

// O reconhecimento de fala e a geracao de voz exigem versoes incompativeis do
// transformers (>=5.10 e ==5.2.0). Cada um roda em um venv proprio criado sobre o
// ambiente principal com --system-site-packages: assim os dois reaproveitam o mesmo
// torch/CUDA e cada venv custa poucas centenas de MB em vez de outro CUDA inteiro.
const SATELLITES = [
  {
    id: "asr",
    dir: "asr",
    label: "reconhecimento de fala",
    packages: ["transformers>=5.10", "huggingface_hub>=1.0", "soundfile", "librosa"]
  },
  {
    id: "tts",
    dir: "tts",
    label: "geração de voz",
    packages: [
      "transformers==5.2.0",
      "chatterbox-tts==0.1.7 --no-deps",
      "s3tokenizer",
      "diffusers==0.29.0",
      "conformer==0.3.2",
      "resemble-perth",
      "pyloudnorm",
      "omegaconf",
      "safetensors",
      "soundfile",
      "librosa==0.11.0"
    ]
  }
];

// Percentual acumulado (0-100) no inicio de cada etapa do bootstrap, usado para desenhar a barra de progresso.
const PROGRESS = {
  downloadRuntime: 0,
  installRuntime: 5,
  createBackendEnv: 8,
  installBackendDeps: 12,
  createSatellites: 62,
  done: 100
};

function exists(file) { return fssync.existsSync(file); }

// Cria um emissor de progresso que interpola message/detail fixos entre "start" e "end" conforme fraction (0-1).
function reportRange(report, message, detail, start, end) {
  report(message, detail, start);
  return (fraction) => report(message, detail, Math.round(start + (end - start) * Math.min(Math.max(fraction, 0), 1)));
}

function run(command, args, { cwd, onOutput } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, windowsHide: true });
    let output = "";
    const consume = (data) => { output += data.toString(); onOutput?.(data.toString()); };
    child.stdout.on("data", consume);
    child.stderr.on("data", consume);
    child.on("error", reject);
    child.on("close", (code) => code === 0 ? resolve(output) : reject(new Error(`${path.basename(command)} falhou (${code}): ${output.slice(-1200)}`)));
  });
}

function download(url, target, onProgress) {
  return new Promise((resolve, reject) => {
    const request = (current) => https.get(current, { headers: { "User-Agent": "OpenDub" } }, (response) => {
      if (response.statusCode >= 300 && response.statusCode < 400 && response.headers.location) {
        response.resume();
        request(new URL(response.headers.location, current));
        return;
      }
      if (response.statusCode !== 200) { reject(new Error(`Download falhou (${response.statusCode})`)); return; }
      const total = Number(response.headers["content-length"]) || 0;
      let received = 0;
      if (total > 0) {
        response.on("data", (chunk) => {
          received += chunk.length;
          onProgress?.(received / total);
        });
      }
      const stream = fssync.createWriteStream(target);
      response.pipe(stream);
      stream.on("finish", () => stream.close(resolve));
      stream.on("error", reject);
    }).on("error", reject);
    request(url);
  });
}

// O marcador guarda a impressao digital do que foi instalado. Antes ele era so um
// "ja instalei": numa maquina que ja tinha rodado uma versao anterior, o bootstrap
// pulava o pip install e o app subia com as dependencias velhas do update passado.
async function readMarker(file) {
  try { return (await fs.readFile(file, "utf8")).trim(); } catch { return null; }
}

function fingerprint(parts) {
  return crypto.createHash("sha256").update(parts.join("\n")).digest("hex").slice(0, 16);
}

async function ensureMiniforge(runtimeDir, report) {
  const root = path.join(runtimeDir, "miniforge3");
  const conda = path.join(root, "Scripts", "conda.exe");
  if (exists(conda)) return conda;
  await fs.mkdir(runtimeDir, { recursive: true });
  const installer = path.join(runtimeDir, "miniforge-installer.exe");
  const onDownloadProgress = reportRange(report, "Baixando dependências", "Isso acontece somente na primeira abertura.", PROGRESS.downloadRuntime, PROGRESS.installRuntime);
  await download(MINIFORGE_URL, installer, onDownloadProgress);
  report("Instalando dependências", "Preparando os componentes locais do aplicativo.", PROGRESS.installRuntime);
  await run(installer, ["/S", `/D=${root}`]);
  await fs.rm(installer, { force: true });
  if (!exists(conda)) throw new Error("A instalação das dependências não foi concluída.");
  return conda;
}

async function ensureEnvironment(conda, prefix, version, report, percent) {
  const python = path.join(prefix, "python.exe");
  if (exists(python)) return python;
  report("Preparando dependências", "Isso pode levar alguns minutos.", percent);
  await run(conda, ["create", "--prefix", prefix, `python=${version}`, "-y"], { onOutput: () => {} });
  return python;
}

async function pip(python, args) { await run(python, ["-m", "pip", ...args]); }

// Cada satelite herda o torch do ambiente principal (--system-site-packages) e so
// instala por cima o que precisa em versao propria.
async function ensureSatellite(mainPython, runtimeDir, satellite, report, percent) {
  const prefix = path.join(runtimeDir, satellite.dir);
  const python = path.join(prefix, "Scripts", "python.exe");
  const marker = path.join(runtimeDir, `.${satellite.id}-ready`);
  const expected = fingerprint(satellite.packages);
  if (exists(python) && (await readMarker(marker)) === expected) return python;

  report("Instalando dependências", `Preparando o componente de ${satellite.label}.`, percent);
  if (!exists(python)) {
    await run(mainPython, ["-m", "venv", "--system-site-packages", prefix]);
  }
  await pip(python, ["install", "--upgrade", "pip"]);
  for (const entry of satellite.packages) {
    await pip(python, ["install", ...entry.split(" ")]);
  }
  await fs.writeFile(marker, expected);
  return python;
}

// O Seed-VC saiu do produto quando a clonagem passou a ser nativa do TTS. Quem ja tinha
// aberto uma versao anterior ficou com um ambiente conda inteiro (com CUDA) parado no
// disco, mais o repositorio clonado -- alguns GB que nada mais usa.
async function removeSeedVcLeftovers(runtimeDir) {
  for (const leftover of ["seedvc", "seed-vc", ".seedvc-ready"]) {
    await fs.rm(path.join(runtimeDir, leftover), { recursive: true, force: true }).catch(() => {});
  }
}

async function ensureRuntime({ runtimeDir, backendDir, report }) {
  const conda = await ensureMiniforge(runtimeDir, report);
  await removeSeedVcLeftovers(runtimeDir);
  const python = await ensureEnvironment(conda, path.join(runtimeDir, "backend"), "3.11", report, PROGRESS.createBackendEnv);

  const requirementsPath = path.join(backendDir, "requirements.txt");
  const requirements = await fs.readFile(requirementsPath, "utf8");
  const backendMarker = path.join(runtimeDir, ".backend-ready");
  const backendExpected = fingerprint([requirements, ...TORCH_PACKAGES, TORCH_INDEX, ...CONDA_PACKAGES]);
  if ((await readMarker(backendMarker)) !== backendExpected) {
    report("Instalando dependências", "Esta é a etapa mais longa da primeira abertura.", PROGRESS.installBackendDeps);
    // O deno acompanha o ffmpeg porque o yt-dlp precisa de um interpretador JavaScript:
    // sem ele, a extracao do YouTube cai no caminho descontinuado, alguns formatos somem
    // e a verificacao de "nao sou um robo" passa a barrar o download com muito mais
    // frequencia. O yt-dlp procura o deno sozinho no PATH, entao basta estar instalado.
    await run(conda, ["install", "--prefix", path.join(runtimeDir, "backend"), "-c", "conda-forge", ...CONDA_PACKAGES, "-y"]);
    await pip(python, ["install", "--upgrade", "pip"]);
    await pip(python, ["install", "--index-url", TORCH_INDEX, ...TORCH_PACKAGES]);
    await pip(python, ["install", "-r", requirementsPath]);
    await fs.writeFile(backendMarker, backendExpected);
  }

  const satellites = {};
  const span = (PROGRESS.done - PROGRESS.createSatellites) / SATELLITES.length;
  for (const [index, satellite] of SATELLITES.entries()) {
    satellites[satellite.id] = await ensureSatellite(
      python,
      runtimeDir,
      satellite,
      report,
      Math.round(PROGRESS.createSatellites + index * span)
    );
  }

  report("Tudo pronto", "Iniciando o aplicativo.", PROGRESS.done);
  return { python, asrPython: satellites.asr, ttsPython: satellites.tts };
}

module.exports = { ensureRuntime, run };
