const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("app", {
  platform: process.platform,
  onSetupProgress: (callback) => ipcRenderer.on("setup-progress", (_event, detail) => callback(detail)),
  // Vale para todo download (dublado, original e legenda), com o caminho real do arquivo.
  // Devolve a funcao de cancelar: sem isso, cada re-registro (StrictMode remonta o efeito)
  // deixava um listener antigo vivo e o aviso de "salvo em" aparecia duas vezes por download.
  onDownloadDone: (callback) => {
    const handler = (_event, detail) => callback(detail);
    ipcRenderer.on("download-done", handler);
    return () => ipcRenderer.removeListener("download-done", handler);
  },
  openPath: (filePath) => ipcRenderer.invoke("open-path", filePath),
  // Sem filePath, abre a pasta de downloads; com ele, abre e seleciona o arquivo.
  showInFolder: (filePath) => ipcRenderer.invoke("show-in-folder", filePath)
});
