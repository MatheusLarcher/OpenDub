const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("app", {
  platform: process.platform,
  onSetupProgress: (callback) => ipcRenderer.on("setup-progress", (_event, detail) => callback(detail)),
  // Vale para todo download (dublado, original e legenda), com o caminho real do arquivo.
  onDownloadDone: (callback) => ipcRenderer.on("download-done", (_event, detail) => callback(detail)),
  openPath: (filePath) => ipcRenderer.invoke("open-path", filePath)
});
