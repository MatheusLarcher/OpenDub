const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("app", {
  platform: process.platform,
  onSetupProgress: (callback) => ipcRenderer.on("setup-progress", (_event, detail) => callback(detail)),
  // Vale para todo download (dublado, original e legenda), com o caminho real do arquivo.
  onDownloadDone: (callback) => ipcRenderer.on("download-done", (_event, detail) => callback(detail)),
  openPath: (filePath) => ipcRenderer.invoke("open-path", filePath),
  // Sem filePath, abre a pasta de downloads; com ele, abre e seleciona o arquivo.
  showInFolder: (filePath) => ipcRenderer.invoke("show-in-folder", filePath)
});
