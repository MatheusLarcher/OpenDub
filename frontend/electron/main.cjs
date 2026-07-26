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
  backendProcess = spawn(runtime.python, ["-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "5501"], {
    cwd: process.resourcesPath,
    windowsHide: true,
    env: {
      ...process.env,
      PYTHONPATH: process.resourcesPath,
      PATH: `${path.dirname(runtime.python)};${path.join(path.dirname(runtime.python), "Library", "bin")};${process.env.PATH}`,
      OPENDUB_DATA_DIR: dataDir,
      // Reconhecimento de fala e geracao de voz rodam em ambientes proprios.
      OPENDUB_ASR_PYTHON: runtime.asrPython,
      OPENDUB_TTS_PYTHON: runtime.ttsPython
    }
  });
  backendProcess.on("exit", () => { backendProcess = undefined; });
  await waitForBackend();
}

const ICON_PATH = path.join(__dirname, "..", "build", "icon.ico");

ipcMain.handle("open-path", (_event, filePath) => shell.openPath(filePath));

// Abrir a pasta de downloads sem o usuario precisar procurar. Com um arquivo conhecido,
// showItemInFolder abre a pasta JA com ele selecionado, o que responde direto a pergunta
// "onde foi salvo"; sem arquivo, abre a pasta mesmo.
ipcMain.handle("show-in-folder", (_event, filePath) => {
  if (filePath && fs.existsSync(filePath)) {
    shell.showItemInFolder(filePath);
    return "";
  }
  return shell.openPath(app.getPath("downloads"));
});

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

  // TODO download precisa de um caminho explicito: sem item.setSavePath() o Electron
  // fica esperando uma decisao que nunca chega e o arquivo trava na pasta Downloads
  // como um .tmp de nome aleatorio -- foi o que acontecia com a legenda, a transcricao
  // e o video original, que antes caiam no fluxo padrao.
  win.webContents.session.on("will-download", (_event, item) => {
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
    // O botao "Abrir video" so vale para o video dublado; o aviso de "salvo em" vale
    // para todos, senao o usuario clica em baixar e nada indica que o arquivo chegou.
    const isDubbedVideo = item.getURL().includes("/export/video/");
    item.once("done", (_doneEvent, state) => {
      if (win.isDestroyed()) return;
      win.webContents.send("download-done", {
        ok: state === "completed",
        path: item.getSavePath(),
        name: path.basename(item.getSavePath()),
        folder: path.basename(downloadsDir),
        isDubbedVideo
      });
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
    // A dublagem depende de GPU: os modelos de fala rodam em CUDA. Avisar antes é melhor
    // do que deixar o usuário esperar para falhar só quando clicar em "Dublar".
    if (!await hasNvidiaDriver()) {
      await dialog.showMessageBox({
        type: "warning",
        title: "Placa de vídeo não encontrada",
        message: "Não encontramos uma placa de vídeo NVIDIA neste computador.",
        detail: "O OpenDub abre normalmente, mas a dublagem precisa de uma placa NVIDIA para funcionar."
      });
    }
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

// O backend e um processo separado: se ele sobrevive ao app, fica ocupando a porta 5501 e
// a proxima abertura acha que "subiu" mas conversa com a versao velha. Encerramos nos dois
// eventos porque window-all-closed nao dispara quando o app e fechado por outro caminho.
const stopBackend = () => {
  if (!backendProcess) return;
  const current = backendProcess;
  backendProcess = undefined;
  current.kill();
};

app.on("before-quit", stopBackend);

app.on("window-all-closed", () => {
  stopBackend();
  if (process.platform !== "darwin") app.quit();
});
