const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("app", {
  platform: process.platform,
  onSetupProgress: (callback) => ipcRenderer.on("setup-progress", (_event, detail) => callback(detail)),
  onVideoDownloadComplete: (callback) => ipcRenderer.on("video-download-complete", (_event, filePath) => callback(filePath)),
  openPath: (filePath) => ipcRenderer.invoke("open-path", filePath)
});
