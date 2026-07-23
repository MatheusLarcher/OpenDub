const { spawn } = require("child_process");
const fs = require("fs/promises");
const fssync = require("fs");
const https = require("https");
const path = require("path");

const MINIFORGE_URL = "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Windows-x86_64.exe";
const SEED_VC_URL = "https://github.com/Plachtaa/seed-vc/archive/refs/heads/main.zip";
const TORCH_INDEX = "https://download.pytorch.org/whl/cu130";
const TORCH_PACKAGES = ["torch==2.9.1+cu130", "torchaudio==2.9.1+cu130", "torchcodec==0.15.0+cu130"];

// Percentual acumulado (0-100) no inicio de cada etapa do bootstrap, usado para desenhar a barra de progresso.
const PROGRESS = {
  downloadRuntime: 0,
  installRuntime: 5,
  createBackendEnv: 8,
  installBackendDeps: 12,
  createSeedVcEnv: 52,
  downloadSeedVc: 55,
  installSeedVcDeps: 57,
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

async function ensureSeedVc(runtimeDir, conda, backendDir, report) {
  const seedDir = path.join(runtimeDir, "seed-vc");
  const seedPython = await ensureEnvironment(conda, path.join(runtimeDir, "seedvc"), "3.10", report, PROGRESS.createSeedVcEnv);
  if (!exists(path.join(seedDir, "inference.py"))) {
    const onDownloadProgress = reportRange(report, "Baixando dependências", "Necessária para manter a entonação original.", PROGRESS.downloadSeedVc, PROGRESS.installSeedVcDeps);
    const archive = path.join(runtimeDir, "seed-vc.zip");
    await download(SEED_VC_URL, archive, onDownloadProgress);
    await run("powershell.exe", ["-NoProfile", "-Command", `Expand-Archive -LiteralPath '${archive.replace(/'/g, "''")}' -DestinationPath '${runtimeDir.replace(/'/g, "''")}' -Force; Move-Item -LiteralPath '${path.join(runtimeDir, "seed-vc-main").replace(/'/g, "''")}' -Destination '${seedDir.replace(/'/g, "''")}'`]);
    await fs.rm(archive, { force: true });
  }
  const marker = path.join(runtimeDir, ".seedvc-ready");
  if (!exists(marker)) {
    report("Instalando dependências", "A opção de manter a entonação ficará pronta em seguida.", PROGRESS.installSeedVcDeps);
    await pip(seedPython, ["install", "--upgrade", "pip"]);
    await pip(seedPython, ["install", "--index-url", TORCH_INDEX, ...TORCH_PACKAGES]);
    await pip(seedPython, ["install", "-r", path.join(backendDir, "seedvc-requirements.txt")]);
    // resemblyzer depende do pacote "webrtcvad" (sem wheel para Windows). O requirements.txt ja
    // instalou o fork "webrtcvad-wheels", que fornece o mesmo modulo Python; instalar resemblyzer
    // com --no-deps evita que o pip tente buscar/compilar o "webrtcvad" original por cima.
    await pip(seedPython, ["install", "--no-deps", "resemblyzer==0.1.4"]);
    await fs.writeFile(marker, "ok\n");
  }
  return { seedDir, seedPython };
}

async function ensureRuntime({ runtimeDir, backendDir, report }) {
  const conda = await ensureMiniforge(runtimeDir, report);
  const python = await ensureEnvironment(conda, path.join(runtimeDir, "backend"), "3.11", report, PROGRESS.createBackendEnv);
  const marker = path.join(runtimeDir, ".backend-ready");
  if (!exists(marker)) {
    report("Instalando dependências", "Esta é a etapa mais longa da primeira abertura.", PROGRESS.installBackendDeps);
    await run(conda, ["install", "--prefix", path.join(runtimeDir, "backend"), "-c", "conda-forge", "ffmpeg", "-y"]);
    await pip(python, ["install", "--upgrade", "pip"]);
    await pip(python, ["install", "--index-url", TORCH_INDEX, ...TORCH_PACKAGES]);
    await pip(python, ["install", "-r", path.join(backendDir, "requirements.txt")]);
    await fs.writeFile(marker, "ok\n");
  }
  const seed = await ensureSeedVc(runtimeDir, conda, backendDir, report);
  report("Tudo pronto", "Iniciando o aplicativo.", PROGRESS.done);
  return { python, ...seed };
}

module.exports = { ensureRuntime, run };
