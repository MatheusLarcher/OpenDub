const { app, BrowserWindow, dialog, shell, ipcMain } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const http = require("http");
const { ensureRuntime, run } = require("./bootstrap.cjs");

let backendProcess;

function resourcePath(...parts) {
  return app.isPackaged ? path.join(process.resourcesPath, ...parts) : path.join(__dirname, "..", "..", ...parts);
}

function waitForBackend() {
  return new Promise((resolve, reject) => {
    const until = Date.now() + 30000;
    const attempt = () => {
      const request = http.get("http://127.0.0.1:5501/openapi.json", (response) => {
        response.resume();
        if (response.statusCode === 200) resolve(); else retry();
      });
      request.on("error", retry);
    };
    const retry = () => Date.now() >= until ? reject(new Error("O backend não respondeu a tempo.")) : setTimeout(attempt, 400);
    attempt();
  });
}

async function hasNvidiaDriver() {
  try { await run("nvidia-smi.exe", ["-L"]); return true; } catch { return false; }
}

async function startBackend(runtime, dataDir) {
  const backendRoot = resourcePath("backend");
  backendProcess = spawn(runtime.python, ["-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "5501"], {
    cwd: process.resourcesPath,
    windowsHide: true,
    env: {
      ...process.env,
      PYTHONPATH: process.resourcesPath,
      PATH: `${path.dirname(runtime.python)};${path.join(path.dirname(runtime.python), "Library", "bin")};${process.env.PATH}`,
      OPENDUB_DATA_DIR: dataDir,
      SEED_VC_DIR: runtime.seedDir,
      SEED_VC_PYTHON: runtime.seedPython
    }
  });
  backendProcess.on("exit", () => { backendProcess = undefined; });
  await waitForBackend();
}

const ICON_PATH = path.join(__dirname, "..", "build", "icon.ico");

ipcMain.handle("open-path", (_event, filePath) => shell.openPath(filePath));

function createWindow() {
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    autoHideMenuBar: true,
    icon: ICON_PATH,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  win.removeMenu();

  // Links externos (ex: rodape "Feito por LarcherTech AI") abrem no navegador do
  // sistema em vez de virarem uma nova janela do proprio Electron.
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  // So rastreamos o download do video dublado (usado pelo botao "Abrir video"); os
  // outros downloads (legenda, video original) seguem o fluxo padrao do Electron.
  // Escolhemos o caminho explicitamente (em vez de deixar o Electron decidir) porque,
  // sem isso, o download fica pendurado esperando uma decisao que nunca chega.
  win.webContents.session.on("will-download", (_event, item) => {
    if (!item.getURL().includes("/export/video/")) return;
    const downloadsDir = app.getPath("downloads");
    const ext = path.extname(item.getFilename());
    const base = path.basename(item.getFilename(), ext);
    let target = path.join(downloadsDir, `${base}${ext}`);
    let counter = 1;
    while (fs.existsSync(target)) {
      target = path.join(downloadsDir, `${base} (${counter})${ext}`);
      counter += 1;
    }
    item.setSavePath(target);
    item.once("done", (_doneEvent, state) => {
      if (state === "completed") win.webContents.send("video-download-complete", item.getSavePath());
    });
  });

  if (process.env.VITE_DEV_SERVER_URL) {
    win.loadURL(process.env.VITE_DEV_SERVER_URL);
    win.webContents.openDevTools({ mode: "detach" });
  } else {
    win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }
}

function createLoadingWindow() {
  const win = new BrowserWindow({ width: 600, height: 380, resizable: false, frame: false, backgroundColor: "#f4f3ed", icon: ICON_PATH, webPreferences: { preload: path.join(__dirname, "preload.cjs"), contextIsolation: true } });
  win.loadFile(path.join(__dirname, "loading.html"));
  return win;
}

app.whenReady().then(async () => {
  if (process.env.VITE_DEV_SERVER_URL) { createWindow(); return; }
  const loading = createLoadingWindow();
  const report = (message, detail, percent) => loading.webContents.send("setup-progress", { message, detail, percent });
  try {
    const runtimeDir = path.join(app.getPath("userData"), "runtime");
    const dataDir = path.join(app.getPath("userData"), "data");
    const runtime = await ensureRuntime({ runtimeDir, backendDir: resourcePath("backend"), report });
    if (!await hasNvidiaDriver()) report("GPU NVIDIA não detectada", "O aplicativo funcionará, mas a dublagem será mais lenta sem CUDA.", 100);
    report("Iniciando o estúdio", "Só mais um instante.", 100);
    await startBackend(runtime, dataDir);
    createWindow();
    loading.close();
  } catch (error) {
    await dialog.showMessageBox({ type: "error", title: "Não foi possível preparar o OpenDub", message: error.message, detail: "Confira a conexão com a internet e o espaço em disco e abra o aplicativo novamente." });
    app.quit();
    return;
  }

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  backendProcess?.kill();
  if (process.platform !== "darwin") app.quit();
});
