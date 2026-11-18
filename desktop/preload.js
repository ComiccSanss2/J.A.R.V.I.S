const { contextBridge, ipcRenderer } = require("electron");
const os = require("os");

contextBridge.exposeInMainWorld("jarvisDesktop", {
  isDesktop: true,
  platform: process.platform,
  hostname: os.hostname(),
  systemStatus: () => ipcRenderer.invoke("sys:status"),
  openUrl: (url) => ipcRenderer.invoke("sys:openUrl", url),
  openApp: (app) => ipcRenderer.invoke("sys:openApp", app),
  onToggleMute: (cb) => ipcRenderer.on("toggle-mute", () => cb()),
});
